"""Сортировка CRM-вкладок (two-pass) — отдельная команда.

Запускается реже чем crm_sync_incremental (раз в час) потому что
sort это дорогая операция и не критично если ряды некоторое время
неотсортированы.

Использование:
    manage.py crm_sort_all
    manage.py crm_sort_all --tab "Беляева Екатерина"
"""
from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand
import gspread.exceptions

from cargo.models import CrmHawbIndex
from cargo.services.sheets.client import get_client


logger = logging.getLogger('cargo.crm_sort_all')


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'

from cargo.services.sheets.crm_tabs import SPECIALIST_TABS  # noqa: E402  единый whitelist вкладок

COL_HAWB         = 3   # C
COL_ARRIVAL_DATE = 5   # E
LAST_COL         = 24  # X


def _retry(fn, *args, label: str = '', **kwargs):
    import requests.exceptions as _rex
    import urllib3.exceptions as _u3ex
    import ssl as _ssl
    backoff = [1, 2, 4, 8, 16, 32]
    for attempt in range(len(backoff) + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(e.response, 'status_code', None)
            # 409 'operation was aborted' — sort (структурный batch_update,
            # переставляет строки) конфликтует с ПАРАЛЛЕЛЬНОЙ записью в ту же
            # CRM-таблицу (CrmIncSync/CrmSyncHide каждые 5 мин, reindex).
            # Google отклоняет конкурентную операцию; без ретрая sort падал
            # целиком и «сортировки не отрабатывали» (10.07.2026). Ретрай с
            # backoff ловит окно между чужими записями.
            if status in (409, 429, 500, 502, 503, 504) and attempt < len(backoff):
                wait = backoff[attempt]
                logger.warning('crm_sort %s API %s, retry in %ds',
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
                logger.warning('crm_sort %s network err %s: %s, retry in %ds',
                               label, type(e).__name__, str(e)[:120], wait)
                time.sleep(wait)
                continue
            raise


class Command(BaseCommand):
    help = 'Two-pass sort CRM-вкладок.'

    def add_arguments(self, parser):
        parser.add_argument('--tab', help='Только эта вкладка')
        parser.add_argument('--exclude', help='Исключить вкладку (CSV)')

    def handle(self, *args, **opts):
        excluded = set()
        if opts.get('exclude'):
            excluded = {s.strip() for s in opts['exclude'].split(',') if s.strip()}

        client = get_client()
        ss = client.open_by_key(CRM_ID)
        self.stdout.write(f'Spreadsheet: {ss.title}')

        target = []
        for ws in ss.worksheets():
            if ws.title not in SPECIALIST_TABS:
                continue
            if opts['tab'] and ws.title != opts['tab']:
                continue
            if ws.title in excluded:
                continue
            target.append(ws)
        self.stdout.write(f'Sorting tabs: {len(target)}')

        self._sort_all(ss, target)

    def _sort_all(self, ss, target):
        # Батч-кэш ed_status per-tab (не на весь прогон — hourly-команда
        # может идти долго, свежесть статусов важнее межвкладочного кэша).
        from cargo.services.alta.ed_status import ed_status_batch
        for i, ws in enumerate(target):
            try:
                # ТОЛЬКО сортировка. Reindex ВНУТРИ sort убран (10.07.2026):
                # он был главной тяжестью (read всего листа + delete/recreate
                # индекса на каждую вкладку → ~175с/вкладку, 12 вкладок не
                # укладывались в лимит → kill 267014). Его цель (защита от
                # записи incremental в ЧУЖИЕ строки по устаревшему row_index)
                # УСТАРЕЛА: и crm_sync_incremental, и sync_hide_state теперь
                # sort-proof — таргетят живую колонку C (live_row_map), не
                # CrmHawbIndex.row_index. Индекс обновляет отдельный
                # CrmReindex-крон (4×/день). Sort стал секундами.
                with ed_status_batch():
                    self._sort_tab(ss, ws)
            except Exception as e:
                logger.exception('crm_sort tab %s failed', ws.title)
                self.stdout.write(self.style.ERROR(f'  {ws.title}: {e}'))
            if i + 1 < len(target):
                time.sleep(3)

        self.stdout.write(self.style.SUCCESS('Done.'))

    def _reindex_tab(self, ws):
        """После sort row_index'ы стейл. Делаем full reindex (delete +
        recreate) с учётом реального hidden state из Sheets metadata.

        Hide-state для каждого ряда вычисляем заново по DB (если HAWB
        в БД) или по Sheets-state (если non-DB). Это избавляет от
        проблемы матчинга pre-sort vs post-sort при дубликатах с
        разными arrival dates — каждый ряд независимо вычисляет hide.
        """
        from cargo.models import HouseWaybill
        from cargo.services.alta.ed_status import compute_ed_status

        # Read Sheets values + hidden metadata.
        last_col = 24  # X
        rng = f'A1:{chr(ord("A") + last_col - 1)}{ws.row_count}'
        all_vals = _retry(ws.get, rng,
                          value_render_option='UNFORMATTED_VALUE',
                          label=f'{ws.title} reindex get')

        ss = ws.spreadsheet
        meta = _retry(ss.fetch_sheet_metadata,
                      params={
                          'ranges': ws.title,
                          'fields': 'sheets(properties(title),data(rowMetadata(hiddenByUser)))',
                      },
                      label=f'{ws.title} reindex meta')
        hidden_arr = []
        for sh in meta['sheets']:
            if sh['properties']['title'] != ws.title:
                continue
            data = sh.get('data', [])
            if data:
                hidden_arr = [rm.get('hiddenByUser', False)
                              for rm in data[0].get('rowMetadata', [])]
                break

        def _cell(row, idx):
            v = row[idx - 1] if idx - 1 < len(row) else ''
            return str(v).strip() if v not in (None, '') else ''

        # Pre-pass: собираем все HAWB на вкладке для bulk-load из БД.
        hawb_set = set()
        for row in all_vals[1:]:
            if COL_HAWB - 1 >= len(row):
                continue
            hn = str(row[COL_HAWB - 1]).strip()
            if hn:
                hawb_set.add(hn)
        hawbs_db = {
            h.hawb_number: h for h in HouseWaybill.objects
            .filter(hawb_number__in=list(hawb_set))
            .select_related('mawb')
        }

        to_create: list[CrmHawbIndex] = []
        rows_to_hide: list[int] = []
        for i, row in enumerate(all_vals[1:], start=2):
            if COL_HAWB - 1 >= len(row):
                continue
            hn = str(row[COL_HAWB - 1]).strip()
            if not hn:
                continue

            t_raw = (row[20 - 1] if 20 - 1 < len(row) else None)
            t_val = bool(t_raw) if t_raw is not None else False
            is_hidden = hidden_arr[i - 1] if i - 1 < len(hidden_arr) else False

            cur_decl = _cell(row, 23)
            cur_status = _cell(row, 24)

            # Вычисляем will-state как в incremental: для DB-tracked
            # используем DB compute, для non-DB — что в Sheets.
            h = hawbs_db.get(hn)
            if h:
                new_decl = (h.customs_declaration_number or '').strip()
                new_status = compute_ed_status(h) or ''
                if 'Выпуск разрешен' in new_status:
                    will_decl = new_decl
                elif any(m in new_status for m in
                         ('Отказ', 'Отзыв', 'Считается не поданной')):
                    will_decl = ''  # стираем рег.номер у отказа/отзыва
                elif not new_decl:
                    will_decl = ''
                else:
                    will_decl = cur_decl
                will_status = new_status
            else:
                will_decl = cur_decl
                will_status = cur_status

            # Скрываем только выпущенные (отказ/отзыв ребятам нужны).
            is_legacy_released = bool(will_decl) and not will_status
            want_hidden = ('Выпуск разрешен' in will_status
                           or is_legacy_released)

            to_create.append(CrmHawbIndex(
                hawb_number=hn,
                tab_name=ws.title,
                row_index=i,
                last_decl=cur_decl[:64],
                last_status=cur_status[:128],
                last_request=_cell(row, 21),
                last_arrival=_cell(row, 5)[:16],
                last_warehouse=_cell(row, 6)[:32],
                last_t=t_val,
                last_hidden=want_hidden,
            ))
            if want_hidden and not is_hidden:
                # Должен быть hidden, в реальном Sheets visible — re-hide
                rows_to_hide.append(i)

        # Atomic: delete + recreate в одной транзакции с retry на DB lock.
        # Раньше delete был отдельно — при падении bulk_create на DB lock
        # индекс оставался пустым.
        if to_create:
            from django.db import OperationalError, transaction
            backoff = [1, 2, 4, 8, 16, 32, 60]
            for attempt in range(len(backoff) + 1):
                try:
                    with transaction.atomic():
                        CrmHawbIndex.objects.filter(
                            tab_name=ws.title).delete()
                        CrmHawbIndex.objects.bulk_create(
                            to_create, batch_size=500)
                    break
                except OperationalError as e:
                    if 'database is locked' in str(e) and attempt < len(backoff):
                        wait = backoff[attempt]
                        self.stdout.write(self.style.WARNING(
                            f'  {ws.title}: DB lock, retry in {wait}s'))
                        time.sleep(wait)
                        continue
                    raise
            self.stdout.write(
                f'  {ws.title}: reindex {len(to_create)} entries')

        if rows_to_hide:
            self._apply_hide(ws, rows_to_hide)
            self.stdout.write(
                f'  {ws.title}: re-hidden {len(rows_to_hide)} rows')

    def _apply_hide(self, ws, rows: list[int]):
        from collections import deque
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
                'properties': {'hiddenByUser': True},
                'fields': 'hiddenByUser',
            }
        } for s, e in ranges]
        # Chunk по 100 requests.
        ss = ws.spreadsheet
        for i in range(0, len(requests), 100):
            _retry(ss.batch_update, {'requests': requests[i:i + 100]},
                   label=f'{ws.title} rehide')

    def _sort_tab(self, ss, ws):
        n_hawb_rows = CrmHawbIndex.objects.filter(tab_name=ws.title).count()
        self.stdout.write(f'  {ws.title}: HAWB rows in index = {n_hawb_rows}')

        # КРИТИЧНО: Google Sheets sortRange игнорирует HIDDEN ряды
        # (hiddenByUser=True остаются на своих позициях, в сорт не
        # включаются). Чтобы sort работал корректно — сначала UNHIDE
        # всё, сортируем, потом RE-HIDE по индексу (last_hidden=True).
        # Без unhide pass1 не утопит blank ряды вниз — они застрянут
        # между hidden HAWB.
        unhide_req = {
            'updateDimensionProperties': {
                'range': {
                    'sheetId': ws.id,
                    'dimension': 'ROWS',
                    'startIndex': 1,
                    'endIndex': ws.row_count,
                },
                'properties': {'hiddenByUser': False},
                'fields': 'hiddenByUser',
            }
        }

        # Pass 1: HAWB ASC по всему диапазону → пустые-HAWB вниз.
        req_pass1 = {
            'sortRange': {
                'range': {
                    'sheetId': ws.id,
                    'startRowIndex': 1,
                    'endRowIndex': ws.row_count,
                    'startColumnIndex': 0,
                    'endColumnIndex': LAST_COL,
                },
                'sortSpecs': [
                    {'dimensionIndex': COL_HAWB - 1,
                     'sortOrder': 'ASCENDING'},
                ],
            }
        }
        _retry(ss.batch_update, {'requests': [unhide_req, req_pass1]},
               label=f'{ws.title} unhide+pass1')

        # Pass 2: top N (только ряды с HAWB) по arrival ASC + HAWB ASC.
        if n_hawb_rows > 0:
            req_pass2 = {
                'sortRange': {
                    'range': {
                        'sheetId': ws.id,
                        'startRowIndex': 1,
                        'endRowIndex': 1 + n_hawb_rows,
                        'startColumnIndex': 0,
                        'endColumnIndex': LAST_COL,
                    },
                    'sortSpecs': [
                        {'dimensionIndex': COL_ARRIVAL_DATE - 1,
                         'sortOrder': 'ASCENDING'},
                        {'dimensionIndex': COL_HAWB - 1,
                         'sortOrder': 'ASCENDING'},
                    ],
                }
            }
            _retry(ss.batch_update, {'requests': [req_pass2]},
                   label=f'{ws.title} sort pass2')

        # row_index в CrmHawbIndex обновится через _reindex_tab после.
        # Hide перенесли в _reindex_tab — после обновления row_index
        # знаем какие новые ряды надо скрыть.
