from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, HouseWaybill
from cargo.services.alta.ed_status import compute_ed_status


class Command(BaseCommand):
    help = 'Полная диагностика HAWB по рег.номеру ДТ.'

    def add_arguments(self, parser):
        parser.add_argument('decl')

    def handle(self, *args, **opts):
        decl = opts['decl']
        hs = HouseWaybill.objects.filter(customs_declaration_number=decl)
        self.stdout.write(f'HAWB с decl {decl}: {hs.count()}')
        for h in hs:
            self.stdout.write(
                f'\n=== {h.hawb_number} pk={h.pk} ===')
            self.stdout.write(
                f'  status={h.customs_status!r}  release={h.release_date}  '
                f'filed={h.filed_date}')
            self.stdout.write(
                f'  ed_db={getattr(h, "ed_status", None)!r}')
            self.stdout.write(f'  ed_compute={compute_ed_status(h)!r}')
            self.stdout.write(f'  shipment={h.shipment_type!r}  '
                              f'logistics={h.logistics_status!r}  mawb={h.mawb_id}')
            atts = list(h.declaration_attempts.all())
            self.stdout.write(f'  attempts: {len(atts)}')
            for a in atts:
                self.stdout.write(
                    f'    #{a.attempt_number}  decl={a.declaration_number!r}  '
                    f'status={a.status}  release={a.release_date}')
            msgs = AltaInboxMessage.objects.filter(hawb=h).order_by('prepared_at')
            self.stdout.write(f'  inbox msgs: {msgs.count()}')
            for m in msgs[:20]:
                pm = m.parsed_meta or {}
                self.stdout.write(
                    f'    {m.prepared_at}  {m.msg_type}  kind={m.msg_kind!r}  '
                    f'dc={pm.get("decision_code")!r}  '
                    f'gtd={pm.get("gtd_number")!r}')
