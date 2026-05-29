"""Inbox-msg где matched HAWB существует, kind=registered, gtd есть,
но у HAWB customs_declaration_number пустой → требуется re-dispatch.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    help = 'Найти matched registered msgs где у HAWB decl пуст.'

    def add_arguments(self, parser):
        parser.add_argument('--fix', action='store_true')

    def handle(self, *args, **opts):
        from cargo.services.alta.inbox import dispatch
        from cargo.services.sheets.writeback import (
            begin_batch_writeback, end_batch_writeback,
        )

        qs = AltaInboxMessage.objects.filter(
            hawb__isnull=False,
            msg_kind__in=('registered', 'released'),
        ).select_related('hawb')

        broken = []
        for m in qs:
            pm = m.parsed_meta or {}
            gtd = (pm.get('gtd_number') or '').strip()
            if not gtd:
                continue
            cur = (m.hawb.customs_declaration_number or '').strip()
            if cur:
                continue
            broken.append(m)

        self.stdout.write(f'Найдено: {len(broken)}')
        for m in broken[:30]:
            self.stdout.write(
                f'  #{m.pk}  {m.msg_type}  hawb={m.hawb.hawb_number}  '
                f'gtd={(m.parsed_meta or {}).get("gtd_number")!r}')
        if not opts['fix'] or not broken:
            return

        begin_batch_writeback()
        ok = 0
        try:
            for m in broken:
                try:
                    dispatch(m)
                    ok += 1
                except Exception as e:
                    self.stdout.write(f'  ERR #{m.pk}: {e}')
        finally:
            end_batch_writeback()
        self.stdout.write(self.style.SUCCESS(f'Re-dispatched: {ok}'))
