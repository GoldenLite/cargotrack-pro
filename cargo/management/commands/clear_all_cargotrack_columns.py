"""Очистка всех CargoTrack-колонок в Sheets «Общее».

Используется когда юзер случайно поломал порядок строк в Sheets (например,
отсортировал по столбцу без включения остальных) — наши значения остались
напротив чужих HAWB. Чистый сброс + переимпорт + resync восстановит.

Заголовки колонок НЕ удаляются — только содержимое строк данных.

Запуск:
    uv run python manage.py clear_all_cargotrack_columns --dry-run
    uv run python manage.py clear_all_cargotrack_columns
"""
from __future__ import annotations

import time

import gspread.exceptions
from django.core.management.base import BaseCommand

from cargo.models import SheetSource
from cargo.services.sheets.client import SheetsConfigError, open_worksheet


# Все наши заголовки (CARGOTRACK_*). Список зашит, чтобы команда не зависела
# от того что импортируется/экспортируется из writeback.py.
CARGOTRACK_HEADERS = (
    'CargoTrack: ДТ',
    'CargoTrack: лицензия СВХ',
    'CargoTrack: дата подачи ДО1',
    'CargoTrack: дата регистрации ДО1',
    'CargoTrack: рег. номер ДО1',
    'CargoTrack: вес ДО1',
    'CargoTrack: мест ДО1',
    'CargoTrack: дата ДО2',
    'CargoTrack: дата подачи',
    'CargoTrack: дата выпуска',
)


def _col_letter(col_idx: int) -> str:
    result = ''
    n = col_idx
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(ord('A') + rem) + result
    return result


class Command(BaseCommand):
    help = 'Очистить все CargoTrack-колонки во всех general-таблицах'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--source-id', type=int, default=0)

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
            except (SheetsConfigError, gspread.exceptions.APIError) as e:
                self.stdout.write(f'  skip: {e}')
                continue

            # Читаем шапку чтобы найти наши колонки
            try:
                header = ws.row_values(source.header_row)
            except gspread.exceptions.APIError as e:
                self.stdout.write(f'  header read failed: {e}')
                continue

            data_start = source.header_row + 1
            data_end = ws.row_count

            for hdr in CARGOTRACK_HEADERS:
                if hdr not in header:
                    self.stdout.write(f'  «{hdr}»: нет в шапке, пропуск')
                    continue
                col = header.index(hdr) + 1
                letter = _col_letter(col)
                range_str = f'{letter}{data_start}:{letter}{data_end}'

                if opts['dry_run']:
                    self.stdout.write(f'  «{hdr}» col={letter} '
                                      f'[DRY] would clear {range_str}')
                    continue

                try:
                    ws.batch_clear([range_str])
                    self.stdout.write(self.style.SUCCESS(
                        f'  «{hdr}» col={letter}: cleared {range_str}'))
                except gspread.exceptions.APIError as e:
                    self.stdout.write(f'  «{hdr}» clear failed: {e}')
                time.sleep(1)  # soft throttle между колонками
