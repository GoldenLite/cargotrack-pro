"""Bulk-writeback всех EXPORT HouseWaybill в Экспортную статистику."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill
from cargo.services.alta.outbox import _writeback_export_hawbs


class Command(BaseCommand):
    help = 'Bulk-writeback всех EXPORT HAWB в Экспортную статистику.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0)

    def handle(self, *args, **opts):
        qs = HouseWaybill.objects.filter(shipment_type='EXPORT')
        if opts['limit']:
            qs = qs[:opts['limit']]
        hawbs = list(qs)
        self.stdout.write(f'EXPORT HAWB: {len(hawbs)}')
        if not hawbs:
            return
        _writeback_export_hawbs(hawbs)
        self.stdout.write(self.style.SUCCESS('Готово'))
