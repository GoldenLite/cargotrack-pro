"""Полный re-index CRM-вкладок «Рабочее пространство СТО».

Читает все 9 специалист-вкладок, для каждой HAWB обновляет CrmHawbIndex:
- если HAWB уже в индексе на этой вкладке — обновляет row_index +
  last_decl/status/request/arrival/warehouse/hidden из ТЕКУЩИХ Sheets
  значений (это становится baseline для incremental).
- если новая — создаёт запись.
- если HAWB ушла из вкладки (была, нет в текущем читке) — удаляет
  запись (HAWB могла быть удалена или перемещена).

Запускать раз в день ночью (или после большого ручного редактирования
Sheets юзером).

Использование:
    manage.py crm_reindex
    manage.py crm_reindex --tab "Беляева Екатерина"
"""
from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand
from django.utils import timezone as djtz
import gspread.exceptions

from cargo.models import CrmHawbIndex
from cargo.services.sheets.client import get_client


logger = logging.getLogger('cargo.crm_reindex')


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'

SPECIALIST_TABS = {
    'Беляева Екатерина',
    'Калина Елена',
    'Коробкова Екатерина',
    'Азамов Азам',
    'Никонова Светлана',
    'Подолин Алексей',
    'Пругар Ольга',
    'Алексеева Екатерина',
    'Шушарина Татьяна',
}

# Стандартный шаблон CRM-tabs (см. crm_sync.py).
COL_HAWB         = 3   # C
COL_ARRIVAL_DATE = 5   # E
COL_WAREHOUSE    = 6   # F
COL_T            = 20  # T (checkbox «подано/в работе/выпущено»)
COL_REQUEST      = 21  # U
COL_DECL         = 23  # W
COL_ED_STATUS    = 24  # X


def _retry(fn, *args, label: str = '', **kwargs):
    backoff = [1, 2, 4, 8, 16, 32]
    for attempt in range(len(backoff) + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(e.response, 'status_code', None)
            if status in (429, 500, 502, 503, 504) and attempt < len(backoff):
                wait = backoff[attempt]
                logger.warning('crm_reindex %s API %s, retry in %ds',
                               label, status, wait)
                time.sleep(wait)
                continue
            raise


def _col_letter(idx: int) -> str:
    s = ''
    n = idx
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class Command(BaseCommand):
    help = 'Полный reindex CRM-вкладок в CrmHawbIndex.'

    def add_arguments(self, parser):
        parser.add_argument('--tab', help='Только эта вкладка')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        client = get_client()
        ss = client.open_by_key(CRM_ID)
        self.stdout.write(f'Spreadsheet: {ss.title}')

        target_tabs = []
        for ws in ss.worksheets():
            if ws.title not in SPECIALIST_TABS:
                continue
            if opts['tab'] and ws.title != opts['tab']:
                continue
            target_tabs.append(ws)
        self.stdout.write(f'Specialist tabs: {len(target_tabs)}')

        n_total = 0
        for i, ws in enumerate(target_tabs):
            try:
                n_total += self._reindex_tab(ws, opts)
            except Exception as e:
                logger.exception('crm_reindex tab %s failed', ws.title)
                self.stdout.write(self.style.ERROR(f'  {ws.title}: {e}'))
            if i + 1 < len(target_tabs):
                time.sleep(5)

        self.stdout.write(self.style.SUCCESS(
            f'\nTotal indexed HAWB rows: {n_total}'))

    def _reindex_tab(self, ws, opts):
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE(f'=== {ws.title} ==='))

        last_col = max(COL_HAWB, COL_ARRIVAL_DATE, COL_WAREHOUSE,
                       COL_REQUEST, COL_DECL, COL_ED_STATUS)
        last_letter = _col_letter(last_col)
        rng = f'A1:{last_letter}{ws.row_count}'
        all_vals = _retry(ws.get, rng,
                          value_render_option='UNFORMATTED_VALUE',
                          label=f'{ws.title} get')

        # Hidden state per row через Sheets metadata.
        ss = ws.spreadsheet
        meta = _retry(ss.fetch_sheet_metadata,
                      params={
                          'ranges': ws.title,
                          'fields': 'sheets(properties(title),data(rowMetadata(hiddenByUser)))',
                      },
                      label=f'{ws.title} metadata')
        hidden_arr = []
        for sh in meta['sheets']:
            if sh['properties']['title'] != ws.title:
                continue
            data = sh.get('data', [])
            if data:
                hidden_arr = [rm.get('hiddenByUser', False)
                              for rm in data[0].get('rowMetadata', [])]
                break

        # Собираем ВСЕ ряды с HAWB (включая дубликаты в одной вкладке).
        # Ключ: (hawb, row_idx) — row_idx уникален.
        found_rows: list[tuple[str, int, dict, bool]] = []
        for i, row in enumerate(all_vals[1:], start=2):
            if COL_HAWB - 1 >= len(row):
                continue
            hn = str(row[COL_HAWB - 1]).strip()
            if not hn:
                continue

            def _cell(idx):
                v = (row[idx - 1] if idx - 1 < len(row) else '')
                return str(v).strip() if v not in (None, '') else ''

            t_raw = (row[COL_T - 1] if COL_T - 1 < len(row) else None)
            t_val = bool(t_raw) if t_raw is not None else False
            is_hidden = hidden_arr[i - 1] if i - 1 < len(hidden_arr) else False

            found_rows.append((hn, i, {
                'last_decl':      _cell(COL_DECL),
                'last_status':    _cell(COL_ED_STATUS),
                'last_request':   _cell(COL_REQUEST),
                'last_arrival':   _cell(COL_ARRIVAL_DATE),
                'last_warehouse': _cell(COL_WAREHOUSE),
                'last_t':         t_val,
            }, is_hidden))

        n_distinct = len({hn for hn, _, _, _ in found_rows})
        self.stdout.write(
            f'  HAWB rows: {len(found_rows)} (distinct: {n_distinct})')

        if opts['dry_run']:
            self.stdout.write('  --dry-run: skip writes')
            return len(found_rows)

        # Идемпотентный reindex: DELETE все entries вкладки, INSERT новые
        # из текущего Sheets-state. last_hidden корректен из metadata,
        # никакой incremental после не unhide'ит правильное состояние.
        CrmHawbIndex.objects.filter(tab_name=ws.title).delete()

        now = djtz.now()
        to_create: list[CrmHawbIndex] = []
        for hn, row_idx, d, is_hidden in found_rows:
            to_create.append(CrmHawbIndex(
                    hawb_number=hn,
                    tab_name=ws.title,
                    row_index=row_idx,
                    last_decl=d['last_decl'][:64],
                    last_status=d['last_status'][:128],
                    last_request=d['last_request'],
                    last_arrival=d['last_arrival'][:16],
                    last_warehouse=d['last_warehouse'][:32],
                    last_t=d['last_t'],
                    last_hidden=is_hidden,
                ))

        if to_create:
            CrmHawbIndex.objects.bulk_create(to_create, batch_size=500)

        self.stdout.write(f'  reset & created: {len(to_create)}')
        return len(found_rows)
