"""Синкает Sheets W cell с CrmHawbIndex.last_decl.

Цель: когда sort_all/reindex сбрасывает last_decl='' (например для
REJECTED HAWB), но реальная Sheets-ячейка W осталась со старым decl
(потому что sort_all не пишет в W, только в hide). Эта команда
догоняет — читает фактический Sheets и пишет diff.

Только Sheets-операции, никаких DB writes.
"""
import logging
import time

from django.core.management.base import BaseCommand
import gspread.exceptions

from cargo.models import CrmHawbIndex
from cargo.services.sheets.client import get_client


logger = logging.getLogger('cargo.sync_decl')


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'

from cargo.services.sheets.crm_tabs import SPECIALIST_TABS  # noqa: E402  единый whitelist вкладок

COL_HAWB         = 3   # C
COL_DECL         = 23  # W


def _retry(fn, *args, label='', **kwargs):
    backoff = [1, 2, 4, 8, 16, 32]
    for attempt in range(len(backoff) + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(e.response, 'status_code', None)
            if status in (429, 500, 502, 503, 504) and attempt < len(backoff):
                wait = backoff[attempt]
                logger.warning('sync_decl %s API %s, retry %ds',
                               label, status, wait)
                time.sleep(wait)
                continue
            raise


def _col_letter(idx):
    s = ''
    n = idx
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--tab', help='Только эта вкладка')

    def handle(self, *args, **opts):
        client = get_client()
        ss = client.open_by_key(CRM_ID)

        for ws in ss.worksheets():
            if ws.title not in SPECIALIST_TABS:
                continue
            if opts['tab'] and ws.title != opts['tab']:
                continue
            self._sync_tab(ws)
            time.sleep(2)

    def _sync_tab(self, ws):
        self.stdout.write(self.style.NOTICE(f'\n=== {ws.title} ==='))

        # Читаем фактическую колонку W
        col_w_letter = _col_letter(COL_DECL)
        col_c_letter = _col_letter(COL_HAWB)
        # Get W and C colums
        vals = _retry(ws.get,
                      f'{col_c_letter}1:{col_w_letter}{ws.row_count}',
                      value_render_option='UNFORMATTED_VALUE',
                      label=f'{ws.title} get')

        # Map row → (hawb, cur_decl)
        sheets_data = {}
        for i, row in enumerate(vals[1:], start=2):
            if not row:
                continue
            hn = str(row[0]).strip() if row else ''
            if not hn:
                continue
            # cur_decl: смотрим на индекс W в этом подмассиве
            # row начинается с C (idx 0), W это COL_DECL - COL_HAWB = 20-й по счёту
            w_offset = COL_DECL - COL_HAWB
            cur_decl = (str(row[w_offset]).strip()
                        if w_offset < len(row) and row[w_offset] not in (None, '')
                        else '')
            sheets_data[i] = (hn, cur_decl)

        # Сверяем с индексом
        entries = list(CrmHawbIndex.objects.filter(tab_name=ws.title))
        updates = []
        for e in entries:
            sd = sheets_data.get(e.row_index)
            if not sd:
                continue
            sheet_hn, sheet_decl = sd
            if sheet_hn != e.hawb_number:
                # Расхождение по HAWB — пропускаем (нужен reindex)
                continue
            if sheet_decl != e.last_decl:
                updates.append({
                    'range': f'{col_w_letter}{e.row_index}',
                    'values': [[e.last_decl]],
                })

        self.stdout.write(f'  diffs: {len(updates)}')

        if updates:
            CHUNK = 100
            for i in range(0, len(updates), CHUNK):
                _retry(ws.batch_update, updates[i:i + CHUNK],
                       value_input_option='USER_ENTERED',
                       label=f'{ws.title} chunk {i//CHUNK + 1}')
            self.stdout.write(f'  wrote {len(updates)} W cells')
