from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_datetime

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    help = 'Найти pk inbox-msg по prepared_at + type.'

    def add_arguments(self, parser):
        parser.add_argument('--at', required=True)
        parser.add_argument('--type', required=True)

    def handle(self, *args, **opts):
        at = parse_datetime(opts['at'])
        m = AltaInboxMessage.objects.filter(
            prepared_at=at, msg_type=opts['type']).first()
        if m:
            self.stdout.write(f'pk={m.pk}')
        else:
            self.stdout.write('not found')
