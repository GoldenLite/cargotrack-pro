"""Стата CrmHawbIndex по вкладкам."""
from django.core.management.base import BaseCommand
from django.db.models import Count

from cargo.models import CrmHawbIndex


class Command(BaseCommand):
    def handle(self, *args, **opts):
        rows = (CrmHawbIndex.objects
                .values('tab_name')
                .annotate(n=Count('id'))
                .order_by('tab_name'))
        total = 0
        for r in rows:
            self.stdout.write(f'  {r["tab_name"]:40s} = {r["n"]}')
            total += r['n']
        self.stdout.write(f'\nTotal: {total}')
