"""Распределение inbox сообщений по дате (только released)."""
from collections import Counter
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--from', dest='date_from', default='2026-05-14')
        parser.add_argument('--to', dest='date_to', default='2026-06-02')
        parser.add_argument('--msg-type', default='CMN.11350')
        parser.add_argument('--kind', default='released')

    def handle(self, *args, **opts):
        df = date.fromisoformat(opts['date_from'])
        dt = date.fromisoformat(opts['date_to'])
        qs = AltaInboxMessage.objects.filter(
            msg_type=opts['msg_type'],
            msg_kind=opts['kind'],
            prepared_at__date__gte=df,
            prepared_at__date__lte=dt,
        )
        counts = Counter()
        for m in qs.only('prepared_at').iterator(chunk_size=1000):
            counts[m.prepared_at.date()] += 1

        total = sum(counts.values())
        self.stdout.write(
            f'{opts["msg_type"]} {opts["kind"]} '
            f'{df}..{dt}: total={total}')
        d = df
        while d <= dt:
            n = counts.get(d, 0)
            bar = '█' * min(n // 5, 60)
            self.stdout.write(f'  {d}: {n:4d} {bar}')
            d += timedelta(days=1)
