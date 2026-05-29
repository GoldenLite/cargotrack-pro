from django.core.management.base import BaseCommand

from cargo.models import Cargo
from cargo.services.alta.outbox import _writeback_export_hawbs


class Command(BaseCommand):
    help = 'Залить в Экспортную статистику все HAWB указанного Cargo.'

    def add_arguments(self, parser):
        parser.add_argument('awb_number')

    def handle(self, *args, **opts):
        awb = opts['awb_number']
        cargo = Cargo.objects.filter(awb_number=awb).first()
        if not cargo:
            self.stdout.write(self.style.ERROR(f'Cargo {awb} не найден'))
            return
        hawbs = list(cargo.house_waybills.all())
        export_hawbs = [h for h in hawbs
                        if (h.shipment_type or 'IMPORT').upper() == 'EXPORT']
        self.stdout.write(f'Cargo {awb}: всего {len(hawbs)} HAWB, '
                          f'EXPORT {len(export_hawbs)}')
        if not export_hawbs:
            return
        _writeback_export_hawbs(export_hawbs)
        self.stdout.write(self.style.SUCCESS(
            f'Writeback запущен для {len(export_hawbs)} HAWB'))
