"""Re-dispatch CMN.11350/11337/11001 released сообщений для починки
HAWB у которых статус не RELEASED, хотя в БД лежит released.

Принимает список HAWB. Для каждой:
  1) Если есть unmatched (hawb=None,cargo=None) — пробует matched через
     HouseWaybill+MAWB по hawb_number из consignment.
  2) Затем re-dispatch (= повторный inbox.dispatch) для каждого
     released сообщения этой HAWB.
"""
from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, HouseWaybill


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='+')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        from cargo.services.alta.inbox import dispatch

        hawbs_db = {
            h.hawb_number: h for h in HouseWaybill.objects
            .filter(hawb_number__in=opts['hawbs']).select_related('mawb')}

        for waybill in opts['hawbs']:
            h = hawbs_db.get(waybill)
            self.stdout.write(f'\n=== {waybill} ===')
            if not h:
                self.stdout.write('  no HAWB in DB')
                continue
            self.stdout.write(
                f'  before: status={h.customs_status!r} '
                f'decl={h.customs_declaration_number!r}')

            # Все CMN сообщения упоминающие эту HAWB в consignments.
            msgs = []
            qs = AltaInboxMessage.objects.filter(
                msg_type__in=('CMN.11350', 'CMN.11337', 'CMN.11001',
                              'CMN.11309', 'CMN.11010')
            ).order_by('prepared_at')
            for m in qs.iterator(chunk_size=500):
                meta = m.parsed_meta or {}
                hits = False
                for c in meta.get('consignments') or []:
                    if waybill in (c.get('waybills') or []):
                        hits = True
                        break
                if not hits and meta.get('waybill_number') == waybill:
                    hits = True
                if hits:
                    msgs.append(m)

            self.stdout.write(f'  found {len(msgs)} CMN msgs')
            # Сначала auto-match unmatched через HAWB.mawb
            for m in msgs:
                if m.cargo_id is None and m.hawb_id is None and h.mawb:
                    m.cargo = h.mawb
                    m.hawb = h
                    m.save(update_fields=['cargo', 'hawb'])
                    self.stdout.write(
                        f'  auto-matched {m.envelope_id} → '
                        f'cargo={h.mawb.awb_number} hawb={h.hawb_number}')

            if opts['dry_run']:
                continue

            for m in msgs:
                try:
                    dispatch(m)
                    self.stdout.write(
                        f'  dispatched {m.msg_type} env={m.envelope_id[:8]} '
                        f'kind={m.msg_kind} applied={m.status_applied}')
                    if not m.status_applied:
                        err = (m.parsed_meta or {}).get('apply_error')
                        if err:
                            self.stdout.write(f'    apply_error={err!r}')
                except Exception as e:
                    self.stdout.write(self.style.ERROR(
                        f'  FAILED {m.envelope_id}: {e}'))

            h.refresh_from_db()
            self.stdout.write(
                f'  AFTER: status={h.customs_status!r} '
                f'decl={h.customs_declaration_number!r} '
                f'release_date={h.release_date}')
