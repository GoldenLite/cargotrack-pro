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

from cargo.services.sheets.crm_tabs import SPECIALIST_TABS  # noqa: E402  единый whitelist вкладок


def _retry(fn, *args, label='', **kwargs):
    import requests.exceptions as _rex
    import urllib3.exceptions as _u3ex
    import ssl as _ssl
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
        except (_rex.SSLError, _rex.ConnectionError, _rex.ChunkedEncodingError,
                _rex.Timeout, _u3ex.MaxRetryError, _u3ex.ProtocolError,
                _ssl.SSLError, OSError) as e:
            # Network/TLS flake — типично SSL: UNEXPECTED_EOF_WHILE_READING
            # от sheets.googleapis.com. Backoff exponential как для API 5xx.
            if attempt < len(backoff):
                wait = backoff[attempt]
                logger.warning('sync_hide %s network err %s: %s, retry %ds',
                               label, type(e).__name__, str(e)[:120], wait)
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

        # sort-proof: реальные строки HAWB по ЖИВОЙ колонке C — скрываем/
        # раскрываем по фактической позиции, а не по кэшу row_index
        # (менеджеры свободно двигают/удаляют строки между сотрудниками).
        from cargo.services.sheets.crm_realtime import live_row_map
        live = live_row_map(ws)

        # 3. Сверяем — где НУЖНОЕ скрытие ≠ фактическое в Sheets.
        # want_hidden вычисляем ПРЯМО ЗДЕСЬ из кэша decl/status, а не полагаемся
        # на last_hidden (его обновляет hide-фаза crm_sync, которая под лимитами
        # Google API часто НЕ доходит → выпущенные строки оставались раскрытыми).
        # Логика идентична crm_sync_incremental: «Выпуск разрешен» в статусе ИЛИ
        # legacy (есть рег.ДТ, но статуса нет = старая ручная отметка «выпущено»).
        # legacy-ветку применяем ТОЛЬКО к строкам, которых нет в БД (настоящее
        # ручное легаси). Для db-строк реальный статус в last_status; пустой =
        # НЕ выпущена, даже если специалист вписал рег.ДТ в колонку W вручную
        # (ДТ присвоена, выпуска ещё нет) — не скрываем. Иначе ложно прятали
        # невыпущенные (13.07.2026: 10265907412, 10274413851 на Подолине).
        from cargo.models import HouseWaybill
        db_nums = set(HouseWaybill.objects.filter(
            hawb_number__in=[e.hawb_number for e in entries]
        ).values_list('hawb_number', flat=True))
        rows_to_hide = []
        rows_to_show = []
        idx_to_save = []
        for e in entries:
            row = live.get(e.hawb_number)
            if row is None:
                continue  # HAWB больше нет на вкладке — reindex уберёт запись
            want_hidden = ('Выпуск разрешен' in (e.last_status or '')) \
                or (bool(e.last_decl) and not e.last_status
                    and e.hawb_number not in db_nums)
            if want_hidden != e.last_hidden:
                e.last_hidden = want_hidden
                idx_to_save.append(e)
            idx = row - 1
            actual = hidden_arr[idx] if idx < len(hidden_arr) else False
            if want_hidden and not actual:
                rows_to_hide.append(row)
            elif not want_hidden and actual:
                rows_to_show.append(row)

        # Обновляем last_hidden в кэше (best-effort: под DB-lock не критично —
        # Sheets уже поправим ниже, кэш догонит в следующий прогон).
        if idx_to_save:
            try:
                CrmHawbIndex.objects.bulk_update(idx_to_save, ['last_hidden'])
            except Exception as _e:
                logger.warning('sync_hide: last_hidden bulk_update отложен: %s', _e)

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
