"""Очистить customs_declaration_number и filed_date у HAWB со статусом REJECTED.

История остаётся в HawbDeclarationAttempt.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill


class Command(BaseCommand):
    help = 'Очистить decl/filed_date у REJECTED HAWB.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        qs = HouseWaybill.objects.filter(customs_status='REJECTED').exclude(
            customs_declaration_number='')
        self.stdout.write(f'REJECTED HAWB с decl: {qs.count()}')
        for h in qs[:20]:
            self.stdout.write(
                f'  {h.hawb_number}  decl={h.customs_declaration_number}  '
                f'filed={h.filed_date}')
        if opts['dry_run']:
            return
        n = qs.update(customs_declaration_number='', filed_date=None)
        self.stdout.write(self.style.SUCCESS(f'Очищено: {n}'))
