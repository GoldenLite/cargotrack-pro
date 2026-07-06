"""Точечная привязка mawb для EXPORT HAWB с mawb=None.

Для каждого такого HAWB ищем все AltaOutboxObservation типов
CMN.11335/CMN.11349/CMN.11024/CMN.11023 где HAWB упомянут в
parsed_meta.hawbs. Сортируем по prepared_at ASC. Вызываем
_apply_export_outbox(obs) — первое успешное привяжет mawb и
проставит transport_doc в Sheets.
"""
from django.core.management.base import BaseCommand
from cargo.models import HouseWaybill, AltaOutboxObservation


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--limit', type=int, default=0,
                            help='Limit HAWB processed (0=all)')

    def handle(self, *args, **opts):
        from cargo.services.alta.outbox import _apply_export_outbox

        targets = list(HouseWaybill.objects.filter(
            shipment_type='EXPORT', mawb__isnull=True))
        self.stdout.write(f'EXPORT HAWB with mawb=None: {len(targets)}')

        if opts['limit'] > 0:
            targets = targets[:opts['limit']]

        ok = 0
        not_found = 0
        err = 0
        for h in targets:
            # Найти все observation с этим HAWB в hawbs
            obs_list = []
            for o in AltaOutboxObservation.objects.filter(
                    msg_type__in=('CMN.11335', 'CMN.11349',
                                  'CMN.11024', 'CMN.11023')
            ).order_by('prepared_at'):
                pm = o.parsed_meta or {}
                if h.hawb_number in (pm.get('hawbs') or []):
                    obs_list.append(o)

            if not obs_list:
                not_found += 1
                self.stdout.write(
                    self.style.WARNING(f'  {h.hawb_number}: no observation'))
                continue

            if opts['dry_run']:
                self.stdout.write(
                    f'  [dry] {h.hawb_number}: {len(obs_list)} obs candidates')
                ok += 1
                continue

            # Прогон _apply_export_outbox на каждой observation пока mawb не
            # привяжется
            applied = False
            for o in obs_list:
                try:
                    _apply_export_outbox(o)
                    h.refresh_from_db()
                    if h.mawb_id:
                        ok += 1
                        applied = True
                        self.stdout.write(
                            f'  OK {h.hawb_number} -> {h.mawb.awb_number} '
                            f'(via env={o.envelope_id[:8]})')
                        break
                except Exception as e:
                    self.stdout.write(self.style.WARNING(
                        f'  ERR {h.hawb_number} env={o.envelope_id[:8]}: {e}'))
            if not applied:
                err += 1
                self.stdout.write(self.style.WARNING(
                    f'  FAIL {h.hawb_number}: tried {len(obs_list)} obs, '
                    f'no link'))

        self.stdout.write(f'\nDone: ok={ok} err={err} not_found={not_found}')
