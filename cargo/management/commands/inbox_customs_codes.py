"""Распределение CMN.11350 по customs_code."""
from collections import Counter

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--msg-type', default='CMN.11350')

    def handle(self, *args, **opts):
        qs = AltaInboxMessage.objects.filter(msg_type=opts['msg_type'])
        c = Counter()
        for pm in qs.values_list('parsed_meta', flat=True).iterator(chunk_size=1000):
            code = (pm or {}).get('customs_code') or ''
            c[code] += 1
        self.stdout.write(f'{opts["msg_type"]} total: {sum(c.values())}')
        for code, n in c.most_common():
            self.stdout.write(f'  {code or "(empty)":12s} = {n}')
