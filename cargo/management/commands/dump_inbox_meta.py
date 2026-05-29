import json

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    help = 'Дамп parsed_meta inbox-msg.'

    def add_arguments(self, parser):
        parser.add_argument('msg_pk', type=int)

    def handle(self, *args, **opts):
        m = AltaInboxMessage.objects.get(pk=opts['msg_pk'])
        self.stdout.write(
            f'#{m.pk} {m.msg_type} {m.prepared_at}  kind={m.msg_kind!r}  '
            f'hawb_id={m.hawb_id}')
        self.stdout.write(json.dumps(m.parsed_meta or {}, ensure_ascii=False,
                                     default=str, indent=2))
