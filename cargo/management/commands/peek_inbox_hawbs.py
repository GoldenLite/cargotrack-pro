import re
from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    help = 'HAWB-номера упомянутые в raw_xml inbox-msg.'

    def add_arguments(self, parser):
        parser.add_argument('msg_pk', type=int)

    def handle(self, *args, **opts):
        m = AltaInboxMessage.objects.get(pk=opts['msg_pk'])
        self.stdout.write(
            f'#{m.pk} {m.msg_type}  prep={m.prepared_at}  '
            f'kind={m.msg_kind!r}  hawb_id={m.hawb_id}')
        raw = m.raw_xml or ''
        nums = re.findall(
            r'<(?:[\w-]+:)?PrDocumentNumber\b[^>]*>([^<]+)</(?:[\w-]+:)?PrDocumentNumber>',
            raw)
        self.stdout.write(f'PrDocumentNumbers ({len(nums)}):')
        for n in nums[:30]:
            self.stdout.write(f'  {n}')
        pm = m.parsed_meta or {}
        ph = pm.get('providing_hawbs') or []
        if ph:
            self.stdout.write(f'providing_hawbs: {ph}')
