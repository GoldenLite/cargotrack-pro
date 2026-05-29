"""Скопировать release_date с sibling-HAWB.

Логика — см. find_missing_release_propagation:
для HAWB где decl есть, release нет — если sibling по той же ДТ
(через outbox parsed_meta.hawbs) уже RELEASED — копируем release_date
и проставляем RELEASED.
"""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand

from cargo.models import AltaOutboxObservation, HouseWaybill


class Command(BaseCommand):
    help = 'Распространить release_date с sibling-HAWB по одной ДТ.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        from cargo.services.sheets.writeback import (
            begin_batch_writeback, end_batch_writeback,
        )

        obs_qs = AltaOutboxObservation.objects.filter(
            msg_type__in=('CMN.11023', 'CMN.11335',
                          'CMN.11024', 'CMN.11349'))
        siblings: dict[str, set] = defaultdict(set)
        for o in obs_qs:
            pm = o.parsed_meta or {}
            hs = pm.get('hawbs') or []
            for h in hs:
                for hn in hs:
                    if hn != h:
                        siblings[h].add(hn)

        unreleased = HouseWaybill.objects.filter(
            release_date__isnull=True,
        ).exclude(customs_declaration_number='').exclude(
            customs_status='REJECTED').exclude(customs_status='HOLD')

        to_fix = []
        for h in unreleased:
            sibs = siblings.get(h.hawb_number)
            if not sibs:
                continue
            sib = HouseWaybill.objects.filter(
                hawb_number__in=sibs,
                release_date__isnull=False,
                customs_declaration_number=h.customs_declaration_number,
            ).first()
            if not sib:
                # sibling может быть с ДРУГОЙ ДТ (та же ECD, но был
                # отказ-переподача). Допустимо взять любой released sibling
                # с тем же decl.
                continue
            to_fix.append((h, sib))

        self.stdout.write(f'Найдено для пропагации: {len(to_fix)}')
        for h, sib in to_fix[:30]:
            self.stdout.write(
                f'  {h.hawb_number}  decl={h.customs_declaration_number}  '
                f'← {sib.hawb_number}  release={sib.release_date}')
        if opts['dry_run']:
            return

        begin_batch_writeback()
        ok = 0
        try:
            for h, sib in to_fix:
                try:
                    h.refresh_from_db()
                    err = h.change_customs_status(
                        'RELEASED', user=None, event_dt=sib.release_date)
                    if err:
                        self.stdout.write(f'  ERR {h.hawb_number}: {err}')
                    else:
                        ok += 1
                        # register RELEASED attempt
                        from cargo.services.alta.inbox import _register_attempt
                        _register_attempt(h, h.customs_declaration_number)
                except Exception as e:
                    self.stdout.write(f'  EXC {h.hawb_number}: {e}')
        finally:
            end_batch_writeback()
        self.stdout.write(self.style.SUCCESS(f'Пропагировано: {ok}'))
