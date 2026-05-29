"""Перекрасить колонку «CargoTrack: статус ЭД» в «Общее» по текущим значениям.

Аудит-fix писал только текст (audit_sheets_vs_db --fix не цветит). После
изменения логики compute_ed_status многие HAWB перешли в другой статус,
но цвет фона остался старый. Этот пересчёт применяет цвета по текущему
тексту ячейки.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import ImportedSheetRow, SheetSource
from cargo.services.alta.ed_status import bg_color_for_status
from cargo.services.sheets.client import open_worksheet
from cargo.services.sheets.writeback import (
    CARGOTRACK_ED_STATUS_HEADER,
    _retry_api,
)


class Command(BaseCommand):
    help = 'Перекрасить «CargoTrack: статус ЭД» по текущему тексту в Общее.'

    def add_arguments(self, parser):
        parser.add_argument('--chunk', type=int, default=300)

    def handle(self, *args, **opts):
        src = SheetSource.objects.filter(
            kind='general', is_active=True).first()
        if not src:
            self.stdout.write('Нет general-source')
            return
        ws = _retry_api(open_worksheet, src, label='open')
        header = _retry_api(ws.row_values, src.header_row, label='header')
        if CARGOTRACK_ED_STATUS_HEADER not in header:
            self.stdout.write(f'Нет колонки {CARGOTRACK_ED_STATUS_HEADER}')
            return
        col_idx = header.index(CARGOTRACK_ED_STATUS_HEADER) + 1
        # Колонка-буква
        n = col_idx
        letters = ''
        while n:
            n, r = divmod(n - 1, 26)
            letters = chr(65 + r) + letters
        col_letter = letters

        col_vals = _retry_api(ws.col_values, col_idx, label='col_values')
        # Группируем по цвету
        from collections import defaultdict
        by_color: dict = defaultdict(list)
        for i, v in enumerate(col_vals, start=1):
            if i <= src.header_row:
                continue
            val = (v or '').strip()
            if not val:
                continue
            color = bg_color_for_status(val)
            # tuple для hash
            key = (color['red'], color['green'], color['blue'])
            by_color[key].append(i)

        total = sum(len(rs) for rs in by_color.values())
        self.stdout.write(f'К покраске: {total} строк / {len(by_color)} цветов')

        formats: list = []
        for (r, g, b), rows in by_color.items():
            for row in rows:
                formats.append({
                    'range': f'{col_letter}{row}',
                    'format': {
                        'backgroundColor': {'red': r, 'green': g, 'blue': b}
                    },
                })
        # Chunk
        chunk = opts['chunk']
        for i in range(0, len(formats), chunk):
            batch = formats[i:i + chunk]
            try:
                _retry_api(ws.batch_format, batch,
                           label=f'recolor chunk {i // chunk + 1}')
                self.stdout.write(
                    f'  chunk {i // chunk + 1}: {len(batch)} cells')
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f'  chunk {i // chunk + 1} failed: {e}'))
        self.stdout.write(self.style.SUCCESS(f'Готово, {len(formats)} ячеек'))
