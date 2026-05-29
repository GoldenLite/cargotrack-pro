from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill


class Command(BaseCommand):
    help = 'Полный peek HouseWaybill включая EXPORT-specific поля.'

    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='+')

    def handle(self, *args, **opts):
        for hn in opts['hawbs']:
            h = HouseWaybill.objects.filter(hawb_number__iexact=hn).first()
            if not h:
                self.stdout.write(f'{hn}: not found')
                continue
            self.stdout.write(f'=== {hn} pk={h.pk} ===')
            self.stdout.write(f'  shipment_type={h.shipment_type!r}')
            self.stdout.write(f'  logistics={h.logistics_status!r}')
            self.stdout.write(f'  customs_status={h.customs_status!r}')
            self.stdout.write(f'  declaration={h.customs_declaration_number!r}')
            self.stdout.write(f'  filed_date={h.filed_date}')
            self.stdout.write(f'  release_date={h.release_date}')
            self.stdout.write(f'  goods_count={h.goods_count!r}')
            self.stdout.write(f'  declaration_form='
                              f'{getattr(h, "declaration_form", None)!r}')
            self.stdout.write(f'  declarant_name='
                              f'{getattr(h, "declarant_name", None)!r}')
            self.stdout.write(f'  ed_status={getattr(h, "ed_status", None)!r}')
            self.stdout.write(f'  mawb={h.mawb_id}')
