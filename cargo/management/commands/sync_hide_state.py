"""Синкает hidden state Sheets с CrmHawbIndex.last_hidden.

Только Sheets-операции, никаких DB writes (no DB lock risk).
Читает CrmHawbIndex (read-only) + фактический Sheets hidden state,
применяет batchUpdate hide где расходится.
"""
import logging
import time

from django.core.management.base import BaseCommand
import gspread.exceptions

from cargo.models import CrmHawbIndex
from cargo.services.sheets.client import get_client


logger = logging.getLogger('cargo.sync_hide')


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


def _retry(fn, *args, label='', **kwargs):
    backoff = [1, 2, 4, 8, 16, 32]
    for attempt in range(len(backoff) + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(e.response, 'status_code', None)
            if status in (429, 500, 502, 503, 504) and attempt < len(backoff):
                wait = backoff[attempt]
                logger.warning('sync_hide %s API %s, retry %ds',
                               label, status, wait)
                time.sleep(wait)
                continue
            raise


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
            self._sync_tab(ss, ws)
            time.sleep(2)

    def _sync_tab(self, ss, ws):
        self.stdout.write(self.style.NOTICE(f'\n=== {ws.title} ==='))

        # 1. Читаем реальное hidden state Sheets API
        meta = _retry(ss.fetch_sheet_metadata,
                      params={
                          'ranges': ws.title,
                          'fields': 'sheets(properties(title),data(rowMetadata(hiddenByUser)))',
                      },
                      label=f'{ws.title} meta')
        hidden_arr = []
        for sh in meta['sheets']:
            if sh['properties']['title'] != ws.title:
                continue
            data = sh.get('data', [])
            if data:
                hidden_arr = [rm.get('hiddenByUser', False)
                              for rm in data[0].get('rowMetadata', [])]
                break

        # 2. Читаем индекс
        entries = list(CrmHawbIndex.objects.filter(tab_name=ws.title))
        self.stdout.write(f'  index entries: {len(entries)}')

        # 3. Сверяем — где last_hidden ≠ Sheets state
        rows_to_hide = []
        rows_to_show = []
        for e in entries:
            idx = e.row_index - 1
            actual = hidden_arr[idx] if idx < len(hidden_arr) else False
            if e.last_hidden and not actual:
                rows_to_hide.append(e.row_index)
            elif not e.last_hidden and actual:
                rows_to_show.append(e.row_index)

        self.stdout.write(
            f'  to hide: {len(rows_to_hide)}, to show: {len(rows_to_show)}')

        if rows_to_hide:
            self._apply_dim(ss, ws, rows_to_hide, hidden=True)
        if rows_to_show:
            self._apply_dim(ss, ws, rows_to_show, hidden=False)

    def _apply_dim(self, ss, ws, rows, hidden):
        sorted_rows = sorted(set(rows))
        ranges = []
        i = 0
        while i < len(sorted_rows):
            start = sorted_rows[i]
            end = start
            while i + 1 < len(sorted_rows) and sorted_rows[i + 1] == end + 1:
                end = sorted_rows[i + 1]
                i += 1
            i += 1
            ranges.append((start, end))
        requests = [{
            'updateDimensionProperties': {
                'range': {
                    'sheetId': ws.id,
                    'dimension': 'ROWS',
                    'startIndex': s - 1,
                    'endIndex': e,
                },
                'properties': {'hiddenByUser': hidden},
                'fields': 'hiddenByUser',
            }
        } for s, e in ranges]
        # Chunk 100 requests
        for j in range(0, len(requests), 100):
            _retry(ss.batch_update, {'requests': requests[j:j + 100]},
                   label=f'{ws.title} dim')
        action = 'hidden' if hidden else 'shown'
        self.stdout.write(f'  {action} {len(rows)} rows ({len(ranges)} ranges)')
