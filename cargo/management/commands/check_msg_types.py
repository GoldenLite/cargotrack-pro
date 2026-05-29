"""Статистика по типам сообщений в AltaInboxMessage."""
from collections import Counter

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    help = 'Распределение msg_type/msg_kind в AltaInboxMessage'

    def handle(self, *args, **opts):
        counts = Counter()
        for mt, mk in AltaInboxMessage.objects.values_list('msg_type', 'msg_kind'):
            counts[(mt, mk)] += 1
        self.stdout.write(f'TOTAL: {AltaInboxMessage.objects.count()}')
        for (mt, mk), n in sorted(counts.items(), key=lambda x: -x[1]):
            self.stdout.write(f'  {n:6d}  {mt:14s}  kind={mk}')
