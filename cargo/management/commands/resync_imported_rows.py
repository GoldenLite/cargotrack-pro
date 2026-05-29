"""Пересинхронизировать ImportedSheetRow.source_row_index с текущим
положением HAWB в Sheets.

После сортировки/дедупа юзером строки в Sheets двигаются физически, но наш
кеш индексов остаётся стейл. Все последующие batch_write_* стреляют не в те
ячейки.

Алгоритм:
1. Для каждого активного SheetSource:
   - читаем колонку HAWB (для general — «Накладная СДЭК» или похожее)
   - строим карту hawb_number → row_idx из текущего Sheets
2. Для каждой ImportedSheetRow обновляем source_row_index на актуальный.
3. Записи у которых HAWB больше нет в Sheets — пропускаем (можно --delete-missing).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import ImportedSheetRow, SheetSource
from cargo.services.sheets.client import open_worksheet


_HAWB_COL_HINTS = ('накладная', 'hawb', 'номер накладной')


def _find_hawb_col(header: list[str]) -> int:
    """Возвращает 1-based индекс колонки с номером HAWB."""
    for i, h in enumerate(header, start=1):
        low = (h or '').lower()
        for hint in _HAWB_COL_HINTS:
            if hint in low:
                return i
    return 0


class Command(BaseCommand):
    help = ('Пересинхронизировать ImportedSheetRow.source_row_index с '
            'текущими позициями HAWB в Sheets.')

    def add_arguments(self, parser):
        parser.add_argument('--kind', default='',
                            help='Только для этого kind (general/export). '
                                 'Без значения = все активные источники.')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--delete-missing', action='store_true',
                            help='Удалять записи у которых HAWB больше нет '
                                 'в Sheets')

    def handle(self, *args, **opts):
        qs = SheetSource.objects.filter(is_active=True)
        if opts['kind']:
            qs = qs.filter(kind=opts['kind'])
        for src in qs:
            self.stdout.write(f'\n=== {src.kind}/{src.name} ===')
            try:
                ws = open_worksheet(src)
                header = ws.row_values(src.header_row)
            except Exception as e:
                self.stdout.write(f'  open error: {e}')
                continue
            col_idx = _find_hawb_col(header)
            if not col_idx:
                self.stdout.write(f'  не нашёл колонку HAWB. Header: {header}')
                continue
            self.stdout.write(f'  HAWB column: {col_idx} ({header[col_idx-1]!r})')
            try:
                col_vals = ws.col_values(col_idx)
            except Exception as e:
                self.stdout.write(f'  col_values error: {e}')
                continue

            # Карта: HAWB → row_idx (первое вхождение). Дубли логируем.
            sheet_map: dict[str, int] = {}
            dupes = []
            for i, v in enumerate(col_vals, start=1):
                if i <= src.header_row:
                    continue
                key = (v or '').strip()
                if not key:
                    continue
                if key in sheet_map:
                    dupes.append((key, sheet_map[key], i))
                    continue
                sheet_map[key] = i
            self.stdout.write(f'  HAWB в Sheets: {len(sheet_map)}, '
                              f'дубли: {len(dupes)}')

            # Сравниваем с БД.
            rs = ImportedSheetRow.objects.filter(source=src)
            updated, untouched, missing = 0, 0, 0
            for r in rs:
                hn = r.hawb_number_norm
                if not hn:
                    continue
                sheet_idx = sheet_map.get(hn)
                if sheet_idx is None:
                    missing += 1
                    if opts['delete_missing']:
                        if not opts['dry_run']:
                            r.delete()
                    continue
                if r.source_row_index == sheet_idx:
                    untouched += 1
                    continue
                if not opts['dry_run']:
                    r.source_row_index = sheet_idx
                    r.save(update_fields=['source_row_index'])
                updated += 1
            self.stdout.write(self.style.SUCCESS(
                f'  обновлено: {updated}, без изменений: {untouched}, '
                f'нет в Sheets: {missing}'))
            if dupes[:5]:
                self.stdout.write('  --- примеры дублей ---')
                for hn, r1, r2 in dupes[:5]:
                    self.stdout.write(f'    {hn}: rows {r1}, {r2}')
