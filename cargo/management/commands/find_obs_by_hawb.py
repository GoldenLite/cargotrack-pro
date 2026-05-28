"""Найти все AltaOutboxObservation, в parsed_meta которых упомянут указанный HAWB."""
from django.core.management.base import BaseCommand

from cargo.models import AltaOutboxObservation


class Command(BaseCommand):
    help = 'Список outbox-observation для HAWB-номера'

    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='+')

    def handle(self, *args, **opts):
        all_obs = AltaOutboxObservation.objects.filter(
            msg_type__in=['CMN.11023', 'CMN.11349']).order_by('prepared_at')
        for hn in opts['hawbs']:
            self.stdout.write(self.style.NOTICE(f'=== {hn} ==='))
            hits = []
            for o in all_obs:
                pm = o.parsed_meta or {}
                if hn in (pm.get('hawbs') or []):
                    hits.append(o)
            for o in hits:
                pm = o.parsed_meta or {}
                raw = pm.get('raw_xml') or ''
                self.stdout.write(
                    f'  {o.msg_type} env={o.envelope_id} '
                    f'prepared={o.prepared_at} raw={len(raw)} '
                    f'goods_count={pm.get("goods_count", "-")} '
                    f'per_hawb={pm.get("goods_count_per_hawb", "-")}'
                )
            if not hits:
                self.stdout.write('  (нет)')
