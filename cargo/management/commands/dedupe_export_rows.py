"""Дедуп строк export-вкладки: для каждой HAWB оставить одну (лучше
заполненную), остальные очистить + удалить ImportedSheetRow."""
from collections import defaultdict

from django.core.management.base import BaseCommand

from cargo.models import ImportedSheetRow
from cargo.services.sheets.writeback import (
    _get_export_source, open_worksheet, _retry_api, _col_letter,
    EXPORT_HEADERS_ORDER,
)


class Command(BaseCommand):
    help = 'Dedupe строк в Sheets «Экспортная статистика»'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        src = _get_export_source()
        if not src:
            return
        ws = _retry_api(open_worksheet, src, label='dedupe open')
        ncols = len(EXPORT_HEADERS_ORDER)
        last_letter = _col_letter(ncols)
        last_row = ws.row_count

        # Полное чтение значений A:last
        grid = _retry_api(
            ws.get, f'A{src.header_row + 1}:{last_letter}{last_row}',
            label='dedupe get')
        # grid[i] — список ячеек строки (header_row+1 + i)

        rows_by_hawb: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for offset, row in enumerate(grid):
            row_idx = src.header_row + 1 + offset
            hn = (row[0] if row else '').strip()
            if not hn:
                continue
            non_empty = sum(1 for c in row if (c or '').strip())
            rows_by_hawb[hn].append((row_idx, non_empty))

        to_clear_ranges: list[str] = []
        to_delete_ir_ids: list[int] = []
        kept = 0
        cleaned = 0
        for hn, rows in rows_by_hawb.items():
            if len(rows) <= 1:
                continue
            # лучше заполненная строка (max non_empty), при равенстве — меньший row_idx
            rows_sorted = sorted(rows, key=lambda x: (-x[1], x[0]))
            keep_row = rows_sorted[0][0]
            kept += 1
            for row_idx, _ in rows_sorted[1:]:
                to_clear_ranges.append(f'A{row_idx}:{last_letter}{row_idx}')
                cleaned += 1
                # удалить ImportedSheetRow по этому row_idx
                for ir in ImportedSheetRow.objects.filter(
                        source=src, source_row_index=row_idx,
                        hawb_number_norm__iexact=hn):
                    to_delete_ir_ids.append(ir.pk)

        self.stdout.write(
            f'HAWB с дублями: {kept}, строк на очистку: {cleaned}, '
            f'ImportedSheetRow на удаление: {len(to_delete_ir_ids)}')

        if opts['dry_run']:
            return

        # Очистка значений в Sheets — пачками по 100 чтобы не упереться в quota
        CH = 100
        for i in range(0, len(to_clear_ranges), CH):
            chunk = to_clear_ranges[i:i + CH]
            _retry_api(ws.batch_clear, chunk, label='dedupe batch_clear')

        # Удаление ImportedSheetRow
        n = ImportedSheetRow.objects.filter(pk__in=to_delete_ir_ids).delete()
        self.stdout.write(self.style.SUCCESS(
            f'Готово: очищено {cleaned} строк в Sheets, удалено {n[0]} ImportedSheetRow'))
