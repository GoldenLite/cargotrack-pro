"""Найти HAWB которые встречаются в одной вкладке >1 раз."""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db.models import Count

from cargo.models import CrmHawbIndex


class Command(BaseCommand):
    def handle(self, *args, **opts):
        # Group by (hawb, tab) и count > 1
        counts = (CrmHawbIndex.objects
                  .values('hawb_number', 'tab_name')
                  .annotate(n=Count('id'))
                  .filter(n__gt=1)
                  .order_by('tab_name', 'hawb_number'))
        for c in counts:
            entries = CrmHawbIndex.objects.filter(
                hawb_number=c['hawb_number'],
                tab_name=c['tab_name'],
            ).order_by('row_index')
            self.stdout.write(
                f'{c["tab_name"]} / {c["hawb_number"]}: {c["n"]} entries')
            for e in entries:
                self.stdout.write(
                    f'  row={e.row_index} '
                    f'decl={e.last_decl!r} status={e.last_status!r} '
                    f'arrival={e.last_arrival!r} hidden={e.last_hidden} '
                    f't={e.last_t}')
