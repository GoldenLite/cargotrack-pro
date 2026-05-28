"""Быстрая проверка: сохраняет ли свежий агент raw_xml в outbox observation."""
from django.core.management.base import BaseCommand

from cargo.models import AltaOutboxObservation


class Command(BaseCommand):
    help = 'Top-5 свежих CMN.11349/11023 — есть ли raw_xml и goods_count'

    def add_arguments(self, parser):
        parser.add_argument('--types', nargs='+',
                            default=['CMN.11349', 'CMN.11023'])
        parser.add_argument('--limit', type=int, default=5)

    def handle(self, *args, **opts):
        for t in opts['types']:
            self.stdout.write(self.style.NOTICE(f'=== {t} ==='))
            qs = (AltaOutboxObservation.objects
                  .filter(msg_type=t).order_by('-prepared_at')[:opts['limit']])
            for o in qs:
                pm = o.parsed_meta or {}
                raw = pm.get('raw_xml') or ''
                self.stdout.write(
                    f'  {o.envelope_id[:8]}.. prepared={o.prepared_at} '
                    f'raw_xml_len={len(raw)} '
                    f'goods_count={pm.get("goods_count", "-")} '
                    f'per_hawb={pm.get("goods_count_per_hawb", "-")} '
                    f'hawbs={pm.get("hawbs", [])}'
                )
