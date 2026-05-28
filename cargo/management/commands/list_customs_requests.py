"""Показать HAWB у которых есть запросы таможни."""
from collections import Counter

from django.core.management.base import BaseCommand

from cargo.models import HawbCustomsRequest


class Command(BaseCommand):
    help = 'Список HAWB с запросами таможни (HawbCustomsRequest)'

    def handle(self, *args, **opts):
        qs = HawbCustomsRequest.objects.exclude(hawb=None).select_related('hawb')
        total = HawbCustomsRequest.objects.count()
        linked = qs.count()
        self.stdout.write(f'Всего HawbCustomsRequest: {total}')
        self.stdout.write(f'Привязано к HAWB:          {linked}')
        self.stdout.write(f'Не привязано:              {total - linked}')

        c = Counter()
        for r in qs:
            c[r.hawb.hawb_number] += 1
        self.stdout.write(f'\nУникальных HAWB с запросами: {len(c)}')
        for hn, n in sorted(c.items(), key=lambda x: -x[1]):
            self.stdout.write(f'  {hn}: {n} запросов')
