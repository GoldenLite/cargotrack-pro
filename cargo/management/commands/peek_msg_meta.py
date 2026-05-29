"""Показывает parsed_meta + raw_xml fragment для первого сообщения указанного типа."""
from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    help = 'parsed_meta sample for msg_type'

    def add_arguments(self, parser):
        parser.add_argument('msg_type')
        parser.add_argument('--n', type=int, default=1)

    def handle(self, *args, **opts):
        qs = AltaInboxMessage.objects.filter(
            msg_type=opts['msg_type']).order_by('-prepared_at')[:opts['n']]
        for m in qs:
            self.stdout.write(self.style.NOTICE(
                f'\n=== pk={m.pk} {m.msg_type} {m.prepared_at} kind={m.msg_kind} ==='))
            pm = m.parsed_meta or {}
            for k, v in pm.items():
                if k == 'raw_xml':
                    continue
                self.stdout.write(f'  {k}: {v!r}')
            raw = m.raw_xml or ''
            self.stdout.write(f'  raw_xml len={len(raw)}')
            if raw:
                # ищем GTDNumber / RegistrationDate / CustomsCode фрагменты
                import re
                for tag in ('GTDNumber', 'RegistrationDate', 'CustomsCode',
                            'MessageType', 'DecisionCode'):
                    m2 = re.search(
                        r'<(?:[\w-]+:)?' + tag + r'[^>]*>([^<]+)</', raw)
                    if m2:
                        self.stdout.write(f'    {tag}: {m2.group(1)}')
