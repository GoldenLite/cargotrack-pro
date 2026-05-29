"""Полная переинициализация вкладки «Экспортная статистика».

1. Удаляет все физические строки в Sheets кроме шапки.
2. Удаляет все ImportedSheetRow для export-source.
3. Для каждой EXPORT-HAWB в БД — append_row + writeback всех колонок.

Использовать только после cleanup_misclassified_exports когда в БД
остались правильные ЭК.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Reset export-вкладки + полный writeback оставшихся ЭК'

    def handle(self, *args, **opts):
        from cargo.models import HouseWaybill, ImportedSheetRow
        from cargo.services.sheets.writeback import (
            _get_export_source, open_worksheet, _retry_api,
        )
        from cargo.services.alta.outbox import _writeback_export_hawbs

        src = _get_export_source()
        if not src:
            self.stdout.write(self.style.ERROR('Нет SheetSource(kind=export)'))
            return

        # 1. Удаляем физические ряды
        try:
            ws = _retry_api(open_worksheet, src, label='reset open')
            row_count = ws.row_count
            if row_count > src.header_row:
                _retry_api(ws.delete_rows, src.header_row + 1, row_count,
                           label='reset delete_rows')
                self.stdout.write(
                    f'Sheets: удалены строки {src.header_row + 1}..{row_count}')
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'delete_rows failed: {e}'))
            return

        # 2. ImportedSheetRow
        n = ImportedSheetRow.objects.filter(source=src).delete()[0]
        self.stdout.write(f'ImportedSheetRow удалено: {n}')

        # 3. Append + writeback
        hawbs = list(HouseWaybill.objects.filter(shipment_type='EXPORT'))
        self.stdout.write(f'EXPORT HAWB: {len(hawbs)}')
        if hawbs:
            _writeback_export_hawbs(hawbs)
            self.stdout.write(self.style.SUCCESS('Writeback готов'))
