"""Стирает всё содержимое колонки «CargoTrack: ДТ» в Sheets «Общее».

Нужно когда юзер пересортировал/перетасовал строки в Sheets без включения
нашей колонки в сортировку — наши значения остались на физических row,
теперь напротив чужих HAWB. Чистый сброс + последующий resync восстановит.

Шапку (header) не трогаем — её index в кеше остаётся валидным.

Запуск:
    uv run python manage.py clear_cargotrack_column --dry-run
    uv run python manage.py clear_cargotrack_column
    uv run python manage.py clear_cargotrack_column --source-id 1
"""
from __future__ import annotations

import time

import gspread.exceptions
from django.core.management.base import BaseCommand

from cargo.models import SheetSource
from cargo.services.sheets.client import SheetsConfigError, open_worksheet
from cargo.services.sheets.writeback import (
    CARGOTRACK_COL_HEADER, _ensure_cargotrack_column,
)


def _col_letter(col_idx: int) -> str:
    result = ''
    n = col_idx
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(ord('A') + rem) + result
    return result


class Command(BaseCommand):
    help = 'Очистить колонку «CargoTrack: ДТ» во всех general-таблицах'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--source-id', type=int, default=0,
                            help='Только один SheetSource (по id)')

    def handle(self, *args, **opts):
        qs = SheetSource.objects.filter(kind='general')
        if opts['source_id']:
            qs = qs.filter(pk=opts['source_id'])

        sources = list(qs)
        self.stdout.write(f'General-таблиц: {len(sources)}')

        for source in sources:
            self.stdout.write(f'\n=== {source.name} ===')
            try:
                ws = open_worksheet(source)
                col = _ensure_cargotrack_column(ws, source.header_row)
            except (SheetsConfigError, gspread.exceptions.APIError) as e:
                self.stdout.write(f'  skip: {e}')
                continue
            except Exception as e:
                self.stdout.write(f'  error: {e}')
                continue

            letter = _col_letter(col)

            # Прочитать как сейчас — увидим сколько ячеек заполнено
            try:
                col_values = ws.col_values(col)
            except gspread.exceptions.APIError as e:
                self.stdout.write(f'  read failed: {e}')
                continue

            # Считаем непустые ячейки ниже шапки
            non_empty_rows = [
                i + 1 for i, v in enumerate(col_values)
                if i + 1 > source.header_row and (v or '').strip()
            ]
            self.stdout.write(f'  col={letter} ({col}), заполнено ячеек: {len(non_empty_rows)}')

            if opts['dry_run']:
                self.stdout.write('  DRY RUN — не очищаю')
                continue

            if not non_empty_rows:
                continue

            # Чистка через batch_clear — один запрос на диапазон
            data_start = source.header_row + 1
            data_end = len(col_values)  # последняя непустая
            range_str = f'{letter}{data_start}:{letter}{data_end}'
            try:
                ws.batch_clear([range_str])
                self.stdout.write(self.style.SUCCESS(f'  cleared range {range_str}'))
            except gspread.exceptions.APIError as e:
                self.stdout.write(f'  batch_clear failed: {e}')
            time.sleep(1)  # soft throttle
