from django.core.management.base import BaseCommand

from cargo.models import ImportedSheetRow, SheetSource
from cargo.services.sheets.client import open_worksheet


class Command(BaseCommand):
    help = 'Показать содержимое строки Sheets для HAWB по обоим kind.'

    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='+')

    def handle(self, *args, **opts):
        sources = list(SheetSource.objects.filter(is_active=True))
        # Группируем HAWB по источнику; затем 1 worksheet open + 1 header read.
        by_src: dict = {}
        not_found = []
        for hn in opts['hawbs']:
            placed = False
            for src in sources:
                rs = ImportedSheetRow.objects.filter(
                    source=src, hawb_number_norm__iexact=hn)
                for r in rs:
                    by_src.setdefault(src.id, (src, []))
                    by_src[src.id][1].append((hn, r.source_row_index))
                    placed = True
            if not placed:
                not_found.append(hn)

        for src_id, (src, items) in by_src.items():
            self.stdout.write(f'\n=== {src.kind}/{src.name} ===')
            try:
                ws = open_worksheet(src)
                header = ws.row_values(src.header_row)
            except Exception as e:
                self.stdout.write(f'  open/header error: {e}')
                continue
            for hn, row_idx in items:
                self.stdout.write(f'  HAWB {hn}  row={row_idx}')
                try:
                    row_vals = ws.row_values(row_idx)
                    for i, (h, v) in enumerate(zip(header, row_vals), start=1):
                        if v:
                            self.stdout.write(f'    [{i}] {h!r}: {v!r}')
                except Exception as e:
                    self.stdout.write(f'    read error: {e}')
        for hn in not_found:
            self.stdout.write(f'\n=== {hn} ===\n  не найдено в ImportedSheetRow')
