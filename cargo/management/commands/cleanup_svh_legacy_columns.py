"""Чистит ячейки старых СВХ-колонок в Sheets «Общее».

После того как мы поняли что CMN.13029 даёт данные ПРЕДСТАВЛЕНИЯ, а
не ДО1, заголовок колонки «CargoTrack: дата размещения» был
переименован в «CargoTrack: дата ДО1». Но старая колонка с прежним
названием осталась в Sheets с неверными данными (датой регистрации
представления, не ДО1).

Команда:
1. Стирает все значения в колонках из LEGACY_SVH_HEADERS.
2. Шапку оставляет — чтобы юзер мог сам решить: удалить колонку
   вручную или переиспользовать.

После чистки → resync_sheets_svh заполнит уже правильную колонку
«CargoTrack: дата ДО1» правильными датами (из CMN.13010).

Запуск:
    uv run python manage.py cleanup_svh_legacy_columns --dry-run
    uv run python manage.py cleanup_svh_legacy_columns
"""
from __future__ import annotations

import time

import gspread.exceptions
from django.core.management.base import BaseCommand

from cargo.models import SheetSource
from cargo.services.sheets.client import SheetsConfigError, open_worksheet
from cargo.services.sheets.writeback import LEGACY_SVH_HEADERS


def _col_letter(col_idx: int) -> str:
    result = ''
    n = col_idx
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(ord('A') + rem) + result
    return result


class Command(BaseCommand):
    help = 'Очистить ячейки устаревших СВХ-колонок в Sheets «Общее»'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        sources = list(SheetSource.objects.filter(kind='general'))
        self.stdout.write(f'General-таблиц: {len(sources)}')
        self.stdout.write(f'Legacy-заголовки: {LEGACY_SVH_HEADERS}')

        for source in sources:
            self.stdout.write(f'\n=== {source.name} ===')
            try:
                ws = open_worksheet(source)
            except SheetsConfigError as e:
                self.stdout.write(f'  skip: {e}')
                continue
            except Exception as e:
                self.stdout.write(f'  open error: {e}')
                continue

            try:
                header = ws.row_values(source.header_row)
            except gspread.exceptions.APIError as e:
                self.stdout.write(f'  row_values failed: {e}')
                continue

            found_cols: dict[str, int] = {}
            for h in LEGACY_SVH_HEADERS:
                for idx, val in enumerate(header, start=1):
                    if (val or '').strip() == h:
                        found_cols[h] = idx
                        break

            if not found_cols:
                self.stdout.write('  legacy-колонок нет')
                continue

            for h, col in found_cols.items():
                letter = _col_letter(col)
                try:
                    col_values = ws.col_values(col)
                except gspread.exceptions.APIError as e:
                    self.stdout.write(f'  read col {h} failed: {e}')
                    continue

                non_empty = sum(
                    1 for i, v in enumerate(col_values)
                    if i + 1 > source.header_row and (v or '').strip()
                )
                self.stdout.write(f'  «{h}» col={letter} ({col})  заполнено: {non_empty}')

                if opts['dry_run'] or not non_empty:
                    continue

                data_start = source.header_row + 1
                data_end = len(col_values)
                range_str = f'{letter}{data_start}:{letter}{data_end}'
                try:
                    ws.batch_clear([range_str])
                    self.stdout.write(self.style.SUCCESS(f'    cleared {range_str}'))
                except gspread.exceptions.APIError as e:
                    self.stdout.write(f'    batch_clear failed: {e}')
                time.sleep(1)
