"""Поиск AltaInboxMessage в raw_xml по подстроке HAWB-номера.

Если HAWB упоминается в raw_xml — то таможня прислала сообщение про эту
накладную, но возможно matching не привязал msg → hawb (по hawb_id).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    help = 'Поиск AltaInboxMessage по подстроке HAWB в raw_xml.'

    def add_arguments(self, parser):
        parser.add_argument('hawb')

    def handle(self, *args, **opts):
        hn = opts['hawb']
        qs = AltaInboxMessage.objects.filter(
            raw_xml__icontains=hn).order_by('-prepared_at')
        self.stdout.write(f'Найдено: {qs.count()}')
        for m in qs[:30]:
            self.stdout.write(
                f'  #{m.pk}  {m.prepared_at}  {m.msg_type}  '
                f'kind={m.msg_kind!r}  hawb_id={m.hawb_id}  '
                f'gtd={m.gtd_reg_number!r}')
