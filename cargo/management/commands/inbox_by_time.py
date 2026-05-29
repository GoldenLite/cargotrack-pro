from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_datetime

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    help = 'Inbox messages в диапазоне prepared_at.'

    def add_arguments(self, parser):
        parser.add_argument('--from', dest='from_dt', required=True)
        parser.add_argument('--to', dest='to_dt', required=True)
        parser.add_argument('--type', default='')

    def handle(self, *args, **opts):
        f = parse_datetime(opts['from_dt'])
        t = parse_datetime(opts['to_dt'])
        qs = AltaInboxMessage.objects.filter(
            prepared_at__gte=f, prepared_at__lte=t)
        if opts['type']:
            qs = qs.filter(msg_type=opts['type'])
        qs = qs.order_by('prepared_at')
        self.stdout.write(f'Найдено: {qs.count()}')
        for m in qs[:50]:
            pm = m.parsed_meta or {}
            self.stdout.write(
                f'  {m.prepared_at}  {m.msg_type}  kind={m.msg_kind!r}  '
                f'hawb_id={m.hawb_id}  gtd={pm.get("gtd_number")!r}')
