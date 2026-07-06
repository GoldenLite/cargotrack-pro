from django.core.management.base import BaseCommand
from cargo.models import HouseWaybill, AltaInboxMessage, AltaOutboxObservation


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('hawb')

    def handle(self, *args, **opts):
        hn = opts['hawb']
        h = HouseWaybill.objects.filter(hawb_number=hn).first()
        self.stdout.write(f'=== outbox observations for {hn} ===')
        if h:
            for m in AltaOutboxObservation.objects.filter(hawb=h).order_by('received_at'):
                meta = m.parsed_meta or {}
                self.stdout.write(
                    f'  {m.received_at} {m.msg_type} env={m.envelope_id} '
                    f'mawb={m.common_waybill_number} hawb={m.waybill_number} '
                    f'decision={meta.get("decision")} decl={meta.get("declaration_number")}')
        self.stdout.write(f'=== inbox with raw_xml containing {hn} ===')
        for m in AltaInboxMessage.objects.filter(raw_xml__contains=hn).order_by('received_at'):
            meta = m.parsed_meta or {}
            self.stdout.write(
                f'  {m.received_at} {m.msg_type} kind={m.msg_kind} '
                f'applied={m.status_applied} hawb_id={m.hawb_id} '
                f'src={meta.get("source")} decision={meta.get("decision")} '
                f'decl={meta.get("declaration_number")}')
        self.stdout.write(f'=== inbox by envelope/initial of outbox observations ===')
        if h:
            envs = list(AltaOutboxObservation.objects.filter(hawb=h).values_list('envelope_id', flat=True))
            self.stdout.write(f'  outbox envelopes: {envs}')
            for env in envs:
                for m in AltaInboxMessage.objects.filter(raw_xml__contains=env).order_by('received_at'):
                    meta = m.parsed_meta or {}
                    self.stdout.write(
                        f'  resp to {env}: {m.received_at} {m.msg_type} kind={m.msg_kind} '
                        f'applied={m.status_applied} hawb_id={m.hawb_id} '
                        f'decision={meta.get("decision")} decl={meta.get("declaration_number")}')
        self.stdout.write(f'=== siblings of MAWB ===')
        if h and h.mawb:
            sibs = HouseWaybill.objects.filter(mawb=h.mawb).exclude(id=h.id)
            for s in sibs:
                self.stdout.write(
                    f'  sib {s.hawb_number} cs={s.customs_status!r} '
                    f'decl={s.customs_declaration_number!r} release={s.release_date}')
