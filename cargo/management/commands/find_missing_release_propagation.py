"""Найти HAWB у которых:
- есть customs_declaration_number
- release_date пуст / status != RELEASED
- но другая HAWB ИЗ ТОЙ ЖЕ outbox CMN.11023/11335/11024/11349 уже RELEASED.

Это означает: таможня выпустила всю ДТ, sibling получил CMN.11350,
а наш — не получил (или матчинг не сработал). По факту HAWB выпущена.
"""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand

from cargo.models import AltaOutboxObservation, HouseWaybill


class Command(BaseCommand):
    help = 'Найти HAWB у которых sibling по outbox выпущен, а они нет.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0)

    def handle(self, *args, **opts):
        # 1. Собираем outbox: HAWB → set(siblings) по parsed_meta.hawbs
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

        # 2. Для каждой HAWB у которой decl есть но release нет — проверить
        #    есть ли sibling с release_date.
        unreleased = HouseWaybill.objects.filter(
            release_date__isnull=True,
        ).exclude(customs_declaration_number='').exclude(
            customs_status='REJECTED').exclude(customs_status='HOLD')

        broken = []
        for h in unreleased:
            sibs = siblings.get(h.hawb_number)
            if not sibs:
                continue
            sib_qs = HouseWaybill.objects.filter(
                hawb_number__in=sibs,
                release_date__isnull=False,
            )
            sib = sib_qs.first()
            if sib:
                broken.append((h, sib))

        self.stdout.write(f'Найдено: {len(broken)}')
        if opts['limit']:
            broken = broken[:opts['limit']]
        for h, sib in broken[:60]:
            self.stdout.write(
                f'  HAWB {h.hawb_number}  decl={h.customs_declaration_number!r}'
                f'  status={h.customs_status!r}  '
                f'sibling {sib.hawb_number} released={sib.release_date}')
