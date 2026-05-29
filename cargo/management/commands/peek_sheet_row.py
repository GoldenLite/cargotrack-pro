from django.core.management.base import BaseCommand

from cargo.models import ImportedSheetRow, SheetSource
from cargo.services.sheets.client import open_worksheet


class Command(BaseCommand):
    help = 'Показать содержимое строки Sheets для HAWB по обоим kind.'

    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='+')

    def handle(self, *args, **opts):
        sources = list(SheetSource.objects.filter(is_active=True))
        for hn in opts['hawbs']:
            self.stdout.write(f'\n=== {hn} ===')
            found = False
            for src in sources:
                rs = ImportedSheetRow.objects.filter(
                    source=src, hawb_number_norm__iexact=hn)
                for r in rs:
                    found = True
                    self.stdout.write(
                        f'  source={src.kind}/{src.name}  row={r.source_row_index}')
                    try:
                        ws = open_worksheet(src)
                        row_vals = ws.row_values(r.source_row_index)
                        header = ws.row_values(src.header_row)
                        for i, (h, v) in enumerate(zip(header, row_vals), start=1):
                            if v:
                                self.stdout.write(f'    [{i}] {h!r}: {v!r}')
                    except Exception as e:
                        self.stdout.write(f'    read error: {e}')
            if not found:
                self.stdout.write('  не найдено в ImportedSheetRow')
