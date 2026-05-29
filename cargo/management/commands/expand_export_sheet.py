from django.core.management.base import BaseCommand

from cargo.models import SheetSource
from cargo.services.sheets.client import open_worksheet


class Command(BaseCommand):
    help = ('Расширить вкладку Экспортная статистика на N пустых строк '
            '(чтобы append_row было куда писать).')

    def add_arguments(self, parser):
        parser.add_argument('--add', type=int, default=200,
                            help='Сколько строк добавить (default=200)')

    def handle(self, *args, **opts):
        src = SheetSource.objects.filter(kind='export', is_active=True).first()
        if not src:
            self.stdout.write('Нет активного export-source')
            return
        ws = open_worksheet(src)
        before = ws.row_count
        n = opts['add']
        ws.add_rows(n)
        ws2 = open_worksheet(src)
        self.stdout.write(self.style.SUCCESS(
            f'rows: {before} → {ws2.row_count} (+{n})'))
