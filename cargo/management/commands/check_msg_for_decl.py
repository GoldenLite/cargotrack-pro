"""Проверить какие inbox-msg упоминают конкретную пару HAWB+decl
в raw_xml одновременно.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from cargo.models import AltaInboxMessage, HouseWaybill


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('hawb')

    def handle(self, *args, **opts):
        hn = opts['hawb']
        h = HouseWaybill.objects.filter(hawb_number=hn).first()
        if not h:
            self.stdout.write(f'no HAWB {hn}')
            return
        decl = h.customs_declaration_number or ''
        self.stdout.write(f'HAWB {hn} decl={decl!r} status={h.customs_status!r}')

        # FK-linked messages
        fk = AltaInboxMessage.objects.filter(hawb=h)
        self.stdout.write(f'FK-linked: {fk.count()}')
        for m in fk[:5]:
            mentions_self = hn in (m.raw_xml or '')
            mentions_decl = bool(decl) and decl in (m.raw_xml or '')
            self.stdout.write(
                f'  #{m.pk} {m.msg_type} kind={m.msg_kind!r} '
                f'prep={m.prepared_at} mention_self={mentions_self} '
                f'mention_decl={mentions_decl}')

        # raw_xml mentions this hawb
        raw = AltaInboxMessage.objects.filter(raw_xml__icontains=hn).exclude(hawb=h)
        self.stdout.write(f'raw_xml mentions hawb (not FK): {raw.count()}')
        for m in raw[:5]:
            mentions_decl = bool(decl) and decl in (m.raw_xml or '')
            self.stdout.write(
                f'  #{m.pk} {m.msg_type} kind={m.msg_kind!r} '
                f'prep={m.prepared_at} mention_decl={mentions_decl}')

        if decl:
            # raw_xml mentions both this hawb AND this decl
            both = AltaInboxMessage.objects.filter(
                raw_xml__icontains=hn).filter(raw_xml__icontains=decl)
            self.stdout.write(f'raw_xml mentions BOTH hawb+decl: {both.count()}')
            for m in both[:5]:
                self.stdout.write(
                    f'  #{m.pk} {m.msg_type} kind={m.msg_kind!r} '
                    f'prep={m.prepared_at}')
