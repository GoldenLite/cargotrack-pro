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
            except Exception as e:
                logger.exception('crm_sort tab %s failed', ws.title)
                self.stdout.write(self.style.ERROR(f'  {ws.title}: {e}'))
            if i + 1 < len(target):
                time.sleep(3)

        self.stdout.write(self.style.SUCCESS('Done.'))

    def _sort_tab(self, ss, ws):
        n_hawb_rows = CrmHawbIndex.objects.filter(tab_name=ws.title).count()
        self.stdout.write(f'  {ws.title}: HAWB rows in index = {n_hawb_rows}')

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
        _retry(ss.batch_update, {'requests': [req_pass1]},
               label=f'{ws.title} sort pass1')

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

        # ВАЖНО: после sort row_index'ы в CrmHawbIndex стейл!
        # Нужно сразу перестроить индекс (просто пересчитать row_index
        # для каждой HAWB по новому порядку). Делаем это inline, без
        # повторного read Sheets — sort это ДЕТЕРМИНИРОВАННАЯ операция
        # ОДНАКО мы не знаем какой порядок Google выбрал внутри ties.
        # Безопаснее: после crm_sort_all запускать crm_reindex.
        # Альтернатива: ставим флажок is_stale на index, incremental
        # дальше не работает пока не reindex. На первой версии —
        # оставляем reindex после sort_all.
