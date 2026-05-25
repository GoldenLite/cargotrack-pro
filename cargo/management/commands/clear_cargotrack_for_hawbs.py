"""Очищает CargoTrack-колонки в Sheets для конкретного списка HAWB.

В отличие от clear_all_cargotrack_columns (стирает ВСЁ), эта команда
точечно — только те row_idx что соответствуют переданным HAWB.

Используется как первый шаг recovery-цепочки:
  1. clear_cargotrack_for_hawbs --file hawbs.txt
  2. relink_hawbs_from_tsd --file hawbs.txt
  3. refresh_moscow_cargo
  4. reparse_alta_inbox --force-dispatch
  5. redispatch_alta_outbox --type ED.DO1

Запуск:
    uv run python manage.py clear_cargotrack_for_hawbs --file hawbs.txt
    uv run python manage.py clear_cargotrack_for_hawbs --file hawbs.txt --dry-run
"""
from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

from cargo.models import ImportedSheetRow, SheetSource
from cargo.services.sheets.client import open_worksheet


CARGOTRACK_HEADERS = (
    'CargoTrack: ДТ',
    'CargoTrack: номер партии',
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
    help = 'Очистить все CargoTrack-колонки для конкретных HAWB (по списку)'

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True,
                            help='Файл со списком HAWB (по одному на строку)')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        path = opts['file']
        if not os.path.exists(path):
            raise CommandError(f'Файл не найден: {path}')

        with open(path, 'r', encoding='utf-8-sig') as f:
            hawbs = [s.strip() for s in f if s.strip() and not s.startswith('#')]
        if not hawbs:
            raise CommandError('Файл пуст')
        self.stdout.write(f'HAWB в списке: {len(hawbs)}')

        sources = list(SheetSource.objects.filter(kind='general', is_active=True))
        for source in sources:
            self.stdout.write('')
            self.stdout.write(f'=== {source.name} ===')

            # Получить row_idx по HAWB-номерам
            rows = (ImportedSheetRow.objects
                    .filter(source=source, hawb_number_norm__in=hawbs)
                    .values_list('source_row_index', 'hawb_number_norm'))
            ridx_set = {r for r, _ in rows}
            self.stdout.write(f'Найдено rows в Sheets: {len(ridx_set)}')
            if not ridx_set:
                continue

            from cargo.services.sheets.writeback import (
                _retry_api,
                _chunked_batch_update,
                _filter_inrange_updates,
            )
            try:
                ws = _retry_api(open_worksheet, source, label='clear open')
                header = _retry_api(ws.row_values, source.header_row,
                                    label='clear header')
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  открыть/читать: {e}'))
                continue

            # Найти индексы наших колонок
            col_map = {}
            for hdr in CARGOTRACK_HEADERS:
                if hdr in header:
                    col_map[hdr] = header.index(hdr) + 1
            self.stdout.write(f'Найдено CargoTrack-колонок: {len(col_map)}')

            # Сгенерировать updates: для каждой ячейки (row_idx × col) пустую строку
            updates = []
            for ridx in ridx_set:
                for hdr, col_idx in col_map.items():
                    letter = _col_letter(col_idx)
                    updates.append({
                        'range': f'{letter}{ridx}',
                        'values': [['']],
                    })

            if opts['dry_run']:
                self.stdout.write(
                    f'DRY RUN: будет очищено {len(updates)} ячеек '
                    f'({len(ridx_set)} строк × {len(col_map)} колонок)'
                )
                continue

            updates = _filter_inrange_updates(updates, ws, source.name)
            n = _chunked_batch_update(ws, updates, 'clear hawbs', source.name)
            self.stdout.write(self.style.SUCCESS(f'  Очищено: {n} cells'))
