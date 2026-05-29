from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    help = 'Последние N inbox сообщений.'

    def add_arguments(self, parser):
        parser.add_argument('--n', type=int, default=10)

    def handle(self, *args, **opts):
        qs = AltaInboxMessage.objects.order_by('-received_at')[:opts['n']]
        for m in qs:
            self.stdout.write(
                f'received={m.received_at}  prep={m.prepared_at}  '
                f'{m.msg_type}  kind={m.msg_kind!r}  hawb_id={m.hawb_id}')
