from django.core.management.base import BaseCommand
from django.db.models import Max

from cargo.models import ImportedSheetRow, SheetSource
from cargo.services.sheets.client import open_worksheet


class Command(BaseCommand):
    help = 'Диагностика Экспортная статистика: размер вкладки vs ImportedSheetRow.'

    def handle(self, *args, **opts):
        src = SheetSource.objects.filter(kind='export', is_active=True).first()
        if not src:
            self.stdout.write('Нет активного export-source')
            return
        ws = open_worksheet(src)
        self.stdout.write(f'Sheet: rows={ws.row_count}  cols={ws.col_count}')

        rs = ImportedSheetRow.objects.filter(source=src)
        agg = rs.aggregate(m=Max('source_row_index'))
        self.stdout.write(f'ImportedSheetRow: total={rs.count()}  max_idx={agg["m"]}')

        over = rs.filter(source_row_index__gt=ws.row_count)
        self.stdout.write(f'Over grid (idx > {ws.row_count}): {over.count()}')
        for r in over[:10]:
            self.stdout.write(
                f'  row={r.source_row_index}  hawb={r.hawb_number_norm}')

        from django.db.models import Count
        dupes = (rs.values('source_row_index')
                   .annotate(c=Count('id'))
                   .filter(c__gt=1)
                   .order_by('-c')[:20])
        self.stdout.write('--- дубли по row_idx ---')
        for d in dupes:
            self.stdout.write(f'  row={d["source_row_index"]}  count={d["c"]}')
