from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    help = 'Дамп raw_xml inbox-message в файл.'

    def add_arguments(self, parser):
        parser.add_argument('msg_pk', type=int)
        parser.add_argument('out_path')

    def handle(self, *args, **opts):
        m = AltaInboxMessage.objects.get(pk=opts['msg_pk'])
        raw = m.raw_xml or ''
        if not raw:
            self.stdout.write(f'#{m.pk} {m.msg_type} — нет raw_xml')
            return
        with open(opts['out_path'], 'w', encoding='utf-8') as f:
            f.write(raw)
        self.stdout.write(
            f'#{m.pk} {m.msg_type} {m.prepared_at}  '
            f'kind={m.msg_kind!r}  saved {len(raw)} chars')
