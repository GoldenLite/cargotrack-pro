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

COL_HAWB         = 3   # C
COL_ARRIVAL_DATE = 5   # E
LAST_COL         = 24  # X


def _retry(fn, *args, label: str = '', **kwargs):
    backoff = [1, 2, 4, 8, 16, 32]
    for attempt in range(len(backoff) + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(e.response, 'status_code', None)
            if status in (429, 500, 502, 503, 504) and attempt < len(backoff):
                wait = backoff[attempt]
                logger.warning('crm_sort %s API %s, retry in %ds',
                               label, status, wait)
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

        for i, ws in enumerate(target):
            try:
                self._sort_tab(ss, ws)
                # После sort row_index'ы в индексе протухли. Reindex
                # читает текущее состояние и обновляет.
                self._reindex_tab(ws)
            except Exception as e:
                logger.exception('crm_sort tab %s failed', ws.title)
                self.stdout.write(self.style.ERROR(f'  {ws.title}: {e}'))
            if i + 1 < len(target):
                time.sleep(3)

        self.stdout.write(self.style.SUCCESS('Done.'))

    def _reindex_tab(self, ws):
        """После sort обновляем row_index в CrmHawbIndex для этой вкладки
        и RE-HIDE ряды у которых last_hidden=True.

        НЕ перетираем last_* — только row_index. Last_* остаются как были,
        чтобы incremental на следующем тике продолжал писать только реальные
        diff'ы.
        """
        rng = f'A1:C{ws.row_count}'
        all_vals = _retry(ws.get, rng,
                          value_render_option='UNFORMATTED_VALUE',
                          label=f'{ws.title} reindex get')
        new_positions: dict[str, int] = {}
        for i, row in enumerate(all_vals[1:], start=2):
            if COL_HAWB - 1 >= len(row):
                continue
            hn = str(row[COL_HAWB - 1]).strip()
            if hn:
                new_positions[hn] = i

        # Bulk-update row_index у существующих записей.
        existing = list(CrmHawbIndex.objects.filter(tab_name=ws.title))
        to_update = []
        rows_to_hide: list[int] = []
        for entry in existing:
            new_idx = new_positions.get(entry.hawb_number)
            if new_idx and new_idx != entry.row_index:
                entry.row_index = new_idx
                to_update.append(entry)
            if entry.last_hidden and new_idx:
                rows_to_hide.append(new_idx)
        if to_update:
            CrmHawbIndex.objects.bulk_update(
                to_update, fields=['row_index'], batch_size=500)
            self.stdout.write(f'  {ws.title}: row_index updated for {len(to_update)}')

        # RE-HIDE ряды которые были скрыты (по last_hidden).
        if rows_to_hide:
            self._apply_hide(ws, rows_to_hide)
            self.stdout.write(f'  {ws.title}: re-hidden {len(rows_to_hide)} rows')

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
