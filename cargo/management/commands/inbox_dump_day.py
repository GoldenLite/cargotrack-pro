"""Дамп всех released CMN.11350 за конкретный день."""
from datetime import date

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('day', help='YYYY-MM-DD')
        parser.add_argument('--msg-type', default='CMN.11350')
        parser.add_argument('--kind', default='released')

    def handle(self, *args, **opts):
        d = date.fromisoformat(opts['day'])
        qs = AltaInboxMessage.objects.filter(
            msg_type=opts['msg_type'],
            msg_kind=opts['kind'],
            prepared_at__date=d,
        ).order_by('prepared_at')

        self.stdout.write(f'{opts["msg_type"]} {opts["kind"]} on {d}: '
                          f'{qs.count()}')
        for m in qs:
            pm = m.parsed_meta or {}
            hawbs = pm.get('hawbs', [])
            customs_code = pm.get('customs_code', '')
            gtd = pm.get('gtd_number', '')
            self.stdout.write(
                f'  #{m.pk} prep={m.prepared_at} '
                f'customs={customs_code} gtd={gtd} '
                f'hawbs={hawbs[:5]}{"..." if len(hawbs) > 5 else ""}')
