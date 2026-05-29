from django.core.management.base import BaseCommand

from cargo.models import ImportedSheetRow, SheetSource
from cargo.services.sheets.client import open_worksheet


class Command(BaseCommand):
    help = ('Удалить ImportedSheetRow для export, у которых '
            'source_row_index превышает текущий размер вкладки. '
            'После чистки повторный writeback пере-аппендит эти HAWB.')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        src = SheetSource.objects.filter(kind='export', is_active=True).first()
        if not src:
            self.stdout.write('Нет активного export-source')
            return
        ws = open_worksheet(src)
        max_row = ws.row_count
        self.stdout.write(f'Sheet rows={max_row}')

        stale = ImportedSheetRow.objects.filter(
            source=src, source_row_index__gt=max_row)
        n = stale.count()
        self.stdout.write(f'Найдено стейл-записей: {n}')
        for r in stale[:20]:
            self.stdout.write(
                f'  row={r.source_row_index}  hawb={r.hawb_number_norm}')
        if opts['dry_run']:
            self.stdout.write('--dry-run: ничего не удаляю')
            return
        if n:
            stale.delete()
            self.stdout.write(self.style.SUCCESS(f'Удалено: {n}'))
