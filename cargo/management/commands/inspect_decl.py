"""Показать все HAWB одной ДТ и их filed_date (UTC + MSK).

Пример: 10001020/250526/0018895 (декларация 10246473928).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone as tz

from cargo.models import HouseWaybill


class Command(BaseCommand):
    help = 'Показать filed_date всех HAWB одной декларации'

    def add_arguments(self, parser):
        parser.add_argument('decl', nargs='+')

    def handle(self, *args, **opts):
        for decl in opts['decl']:
            self.show(decl)
            self.stdout.write('')

    def show(self, decl: str) -> None:
        self.stdout.write(self.style.NOTICE(
            f'\n{"="*60}\n  ДТ: {decl}\n{"="*60}'))

        hawbs = list(HouseWaybill.objects.filter(
            customs_declaration_number=decl
        ).only('hawb_number', 'filed_date').order_by('hawb_number'))
        self.stdout.write(f'HAWB всего: {len(hawbs)}')

        precise = 0
        midnight = 0
        empty = 0
        for h in hawbs:
            if not h.filed_date:
                empty += 1
                continue
            local = tz.localtime(h.filed_date) if tz.is_aware(h.filed_date) else h.filed_date
            is_precise = bool(local.hour or local.minute
                              or local.second or local.microsecond)
            if is_precise:
                precise += 1
            else:
                midnight += 1
            self.stdout.write(
                f'  {h.hawb_number}: {local} '
                f'{"⏰" if is_precise else "🕛"}')
        self.stdout.write(
            f'\nИтого: precise={precise} midnight={midnight} empty={empty}')
