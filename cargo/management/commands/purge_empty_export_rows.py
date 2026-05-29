"""Удалить физически пустые строки в export-вкладке + сдвинуть индексы
ImportedSheetRow на оставшиеся ряды.

После dedupe_export_rows значения в дубликатах были очищены, но сами
строки остались (просто пустые). Физическое удаление через delete_rows
сдвигает индексы — обновляем ImportedSheetRow.source_row_index чтобы
оставшиеся writeback функции работали корректно.
"""
from django.core.management.base import BaseCommand

from cargo.models import ImportedSheetRow
from cargo.services.sheets.writeback import (
    _get_export_source, open_worksheet, _retry_api, _col_letter,
    EXPORT_HEADERS_ORDER,
)


class Command(BaseCommand):
    help = 'Удалить пустые ряды + сдвинуть ImportedSheetRow.source_row_index'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        src = _get_export_source()
        if not src:
            return
        ws = _retry_api(open_worksheet, src, label='purge open')
        ncols = len(EXPORT_HEADERS_ORDER)
        last_letter = _col_letter(ncols)

        # col_values отрезает trailing empty, но между заполненными
        # рядами пустые возвращает как ''. Этого достаточно.
        col_vals = _retry_api(ws.col_values, 1, label='purge col_values')

        # Внутри диапазона данных найти пустые
        empty_rows: list[int] = []
        for idx, v in enumerate(col_vals, start=1):
            if idx <= src.header_row:
                continue
            if not (v or '').strip():
                empty_rows.append(idx)

        # Также «пустые» строки могут быть после последней заполненной
        # (col_values не возвращает них). Возьмём max row_count тоже.
        # Но удаляем только те что меньше или равны last_with_data, чтобы
        # не сжимать «свободные» строки.
        self.stdout.write(f'Найдено пустых строк (внутри данных): {len(empty_rows)}')
        for r in empty_rows[:30]:
            self.stdout.write(f'  row {r}')

        if opts['dry_run']:
            return

        # Удаляем от БОЛЬШЕГО к МЕНЬШЕМУ — индексы выше не сдвигаются
        # при удалении меньшего row.
        empty_rows_sorted = sorted(empty_rows, reverse=True)
        deleted = 0
        from django.db import connection
        for r in empty_rows_sorted:
            try:
                _retry_api(ws.delete_rows, r, label=f'purge delete_rows {r}')
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f'  row {r}: delete_rows failed: {e}'))
                continue
            # Сдвиг индексов в БД: для всех строк > r вычитаем 1.
            with connection.cursor() as cur:
                cur.execute(
                    'UPDATE cargo_importedsheetrow '
                    'SET source_row_index = source_row_index - 1 '
                    'WHERE source_id = %s AND source_row_index > %s',
                    [src.pk, r],
                )
            deleted += 1
            if deleted % 10 == 0:
                self.stdout.write(f'  удалено {deleted}/{len(empty_rows_sorted)}')

        self.stdout.write(self.style.SUCCESS(
            f'Готово: удалено {deleted} рядов'))
