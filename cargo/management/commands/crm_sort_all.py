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
        """После sort row_index'ы стейл, плюс sort мог изменить порядок
        дубликатов. Делаем full reindex (delete + recreate) с учётом
        реального hidden state из Sheets metadata.

        Затем RE-HIDE те ряды у которых после sort должен быть hidden
        (по сохранённому ранее last_hidden ДО sort'а).
        """
        # Сохраняем последний-известный hidden state per (hawb, row_index)
        # ДО полного reset'а индекса.
        pre_sort = {
            (e.hawb_number, e.row_index): e.last_hidden
            for e in CrmHawbIndex.objects.filter(tab_name=ws.title)
        }

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

        # Reset + recreate
        CrmHawbIndex.objects.filter(tab_name=ws.title).delete()

        def _cell(row, idx):
            v = row[idx - 1] if idx - 1 < len(row) else ''
            return str(v).strip() if v not in (None, '') else ''

        to_create: list[CrmHawbIndex] = []
        rows_to_hide: list[int] = []
        # Для матчинга hidden state используем порядок появления HAWB
        # (post-sort стабильный sortRange сохраняет внутри ties).
        hawb_seq: dict[str, int] = {}
        for i, row in enumerate(all_vals[1:], start=2):
            if COL_HAWB - 1 >= len(row):
                continue
            hn = str(row[COL_HAWB - 1]).strip()
            if not hn:
                continue
            # Какой по счёту это hawb (для дублей)
            seq = hawb_seq.get(hn, 0)
            hawb_seq[hn] = seq + 1

            t_raw = (row[20 - 1] if 20 - 1 < len(row) else None)
            t_val = bool(t_raw) if t_raw is not None else False
            is_hidden = hidden_arr[i - 1] if i - 1 < len(hidden_arr) else False

            # Если pre_sort знал hidden state для какого-то row_index с
            # этим HAWB — используем (тот ряд был помечен hide ранее).
            # Берём seq-й по счёту из pre_sort entries для этого HAWB.
            pre_for_hawb = sorted(
                idx for (h, idx) in pre_sort
                if h == hn and pre_sort[(h, idx)])
            should_hide_by_pre = seq < len(pre_for_hawb)

            final_hidden = is_hidden or should_hide_by_pre

            to_create.append(CrmHawbIndex(
                hawb_number=hn,
                tab_name=ws.title,
                row_index=i,
                last_decl=_cell(row, 23)[:64],
                last_status=_cell(row, 24)[:128],
                last_request=_cell(row, 21),
                last_arrival=_cell(row, 5)[:16],
                last_warehouse=_cell(row, 6)[:32],
                last_t=t_val,
                last_hidden=final_hidden,
            ))
            if final_hidden and not is_hidden:
                # pre_sort говорил hide, в реальном Sheets visible — re-hide
                rows_to_hide.append(i)

        if to_create:
            CrmHawbIndex.objects.bulk_create(to_create, batch_size=500)
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
