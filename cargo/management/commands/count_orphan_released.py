"""Подсчитать HAWB с customs_status=RELEASED которые НЕ имеют ни одного
inbox-сообщения (ни по FK, ни через raw_xml.icontains).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from cargo.models import AltaInboxMessage, HouseWaybill


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--list', action='store_true')

    def handle(self, *args, **opts):
        qs = HouseWaybill.objects.filter(customs_status='RELEASED')
        total = qs.count()
        orphan = 0
        orphan_list = []
        for h in qs.only('hawb_number', 'customs_declaration_number'):
            if not h.hawb_number:
                continue
            cond = Q(hawb=h) | Q(raw_xml__icontains=h.hawb_number)
            if not AltaInboxMessage.objects.filter(cond).exists():
                orphan += 1
                if opts['list']:
                    orphan_list.append(
                        (h.hawb_number, h.customs_declaration_number))
        self.stdout.write(f'RELEASED total: {total}')
        self.stdout.write(f'RELEASED orphan (no inbox): {orphan}')
        if opts['list']:
            for hn, d in orphan_list[:200]:
                self.stdout.write(f'  {hn}  decl={d}')
