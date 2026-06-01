"""Проверка какие inbox-msg упоминают HAWB по FK или raw_xml."""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('hawb')

    def handle(self, *args, **opts):
        hn = opts['hawb']
        fk = AltaInboxMessage.objects.filter(hawb__hawb_number=hn)
        raw = AltaInboxMessage.objects.filter(raw_xml__icontains=hn)
        any_q = AltaInboxMessage.objects.filter(
            Q(hawb__hawb_number=hn) | Q(raw_xml__icontains=hn))
        self.stdout.write(f'FK: {fk.count()}')
        self.stdout.write(f'raw_xml: {raw.count()}')
        self.stdout.write(f'OR: {any_q.count()}')
        for m in raw[:5]:
            self.stdout.write(
                f'  #{m.pk}  {m.msg_type}  kind={m.msg_kind!r}  '
                f'hawb_id={m.hawb_id}  prep={m.prepared_at}')
