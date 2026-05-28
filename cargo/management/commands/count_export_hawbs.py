"""Сколько у нас HouseWaybill с shipment_type='EXPORT' и какие они."""
from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill


class Command(BaseCommand):
    help = 'Статистика по EXPORT/IMPORT HAWB'

    def handle(self, *args, **opts):
        total = HouseWaybill.objects.count()
        exp = HouseWaybill.objects.filter(shipment_type='EXPORT')
        imp = HouseWaybill.objects.filter(shipment_type='IMPORT')
        self.stdout.write(f'TOTAL: {total}')
        self.stdout.write(f'EXPORT: {exp.count()}')
        self.stdout.write(f'IMPORT: {imp.count()}')
        if exp.exists():
            self.stdout.write('\nПервые 5 EXPORT HAWB:')
            for h in exp.order_by('-created_at')[:5]:
                self.stdout.write(
                    f'  {h.hawb_number} mawb={h.mawb.awb_number if h.mawb_id else "-"} '
                    f'decl={h.customs_declaration_number or "-"} '
                    f'release={h.release_date or "-"}'
                )
