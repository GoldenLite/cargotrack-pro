from django.core.management.base import BaseCommand
from cargo.models import HouseWaybill, AltaInboxMessage, HawbDeclarationAttempt
from cargo.services.alta.ed_status import compute_ed_status


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='+')

    def handle(self, *args, **opts):
        for hn in opts['hawbs']:
            h = HouseWaybill.objects.filter(hawb_number=hn).first()
            self.stdout.write(f'\n=== {hn} ===')
            if not h:
                self.stdout.write('  NOT IN DB')
                continue
            self.stdout.write(
                f'  type={h.shipment_type} cs={h.customs_status!r} '
                f'decl={h.customs_declaration_number!r} '
                f'release={h.release_date} '
                f'filed={getattr(h, "filed_date", None)} '
                f'mawb={h.mawb.awb_number if h.mawb else None} '
                f'ed_computed={compute_ed_status(h)!r}')
            self.stdout.write('  inbox:')
            for m in AltaInboxMessage.objects.filter(hawb=h).order_by('received_at'):
                meta = m.parsed_meta or {}
                source = meta.get('source') or ''
                self.stdout.write(
                    f'    {m.received_at} {m.msg_type} kind={m.msg_kind} '
                    f'applied={m.status_applied} prepared={m.prepared_at} src={source}')
            self.stdout.write('  attempts:')
            for a in HawbDeclarationAttempt.objects.filter(hawb=h).order_by('attempt_number'):
                self.stdout.write(
                    f'    #{a.attempt_number} decl={a.declaration_number} '
                    f'status={a.status} rd={a.release_date} rj={a.rejected_date}')
