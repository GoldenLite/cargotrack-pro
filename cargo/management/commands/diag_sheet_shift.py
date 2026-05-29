from django.core.management.base import BaseCommand

from cargo.models import ImportedSheetRow, SheetSource
from cargo.services.sheets.client import open_worksheet


class Command(BaseCommand):
    help = 'Сравнить ImportedSheetRow.row_idx с фактическим положением HAWB в колонке.'

    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='+')
        parser.add_argument('--kind', default='general')

    def handle(self, *args, **opts):
        src = SheetSource.objects.filter(
            kind=opts['kind'], is_active=True).first()
        if not src:
            self.stdout.write(f'Нет source kind={opts["kind"]}')
            return
        ws = open_worksheet(src)
        header = ws.row_values(src.header_row)
        # Колонка с номером HAWB (для general — обычно «Номер накладной» или другое)
        # Попробуем найти 'Номер накладной' или взять первую с HAWB-подобным контентом.
        hawb_col_name = None
        for h in header:
            if 'наклад' in (h or '').lower():
                hawb_col_name = h
                break
        if not hawb_col_name:
            self.stdout.write(f'header: {header}')
            self.stdout.write('Не нашёл колонку с накладной')
            return
        hawb_col = header.index(hawb_col_name) + 1
        self.stdout.write(f'HAWB column: {hawb_col} ({hawb_col_name!r})')
        col_vals = ws.col_values(hawb_col)
        # Карта: hawb_number → list[row_idx]
        sheet_map: dict[str, list[int]] = {}
        for i, v in enumerate(col_vals, start=1):
            if i <= src.header_row:
                continue
            key = (v or '').strip()
            if key:
                sheet_map.setdefault(key, []).append(i)
        for hn in opts['hawbs']:
            db_rows = list(ImportedSheetRow.objects.filter(
                source=src, hawb_number_norm__iexact=hn
            ).values_list('source_row_index', flat=True))
            sheet_rows = sheet_map.get(hn, [])
            self.stdout.write(
                f'HAWB {hn}  DB.rows={db_rows}  SHEET.rows={sheet_rows}')
