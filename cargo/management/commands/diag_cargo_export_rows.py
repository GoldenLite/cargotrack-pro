from django.core.management.base import BaseCommand

from cargo.models import Cargo, ImportedSheetRow, SheetSource


class Command(BaseCommand):
    help = 'Сколько HAWB указанного Cargo имеют ImportedSheetRow в export.'

    def add_arguments(self, parser):
        parser.add_argument('awb_number')

    def handle(self, *args, **opts):
        awb = opts['awb_number']
        cargo = Cargo.objects.filter(awb_number=awb).first()
        if not cargo:
            self.stdout.write(f'Cargo {awb} не найден')
            return
        hawbs = list(cargo.hawbs.all())
        src = SheetSource.objects.filter(kind='export', is_active=True).first()
        if not src:
            self.stdout.write('Нет export-source')
            return
        with_row = []
        without_row = []
        for h in hawbs:
            r = ImportedSheetRow.objects.filter(
                source=src,
                hawb_number_norm__iexact=h.hawb_number).first()
            if r:
                with_row.append((h.hawb_number, r.source_row_index))
            else:
                without_row.append(h.hawb_number)
        self.stdout.write(f'Cargo {awb}: HAWB={len(hawbs)}, '
                          f'с row={len(with_row)}, без row={len(without_row)}')
        self.stdout.write('--- без row (первые 20) ---')
        for hn in without_row[:20]:
            self.stdout.write(f'  {hn}')
