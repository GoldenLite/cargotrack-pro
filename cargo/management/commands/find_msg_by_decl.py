"""Поиск inbox-msg по строке decl в raw_xml + проверка latest_inbox."""
from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('search', nargs='+')

    def handle(self, *args, **opts):
        for term in opts['search']:
            self.stdout.write(f'\n=== search: {term!r} ===')
            qs = AltaInboxMessage.objects.filter(
                raw_xml__icontains=term).order_by('-prepared_at')
            self.stdout.write(f'  found: {qs.count()}')
            for m in qs[:10]:
                self.stdout.write(
                    f'  #{m.pk} {m.msg_type} kind={m.msg_kind!r} '
                    f'prep={m.prepared_at}')
