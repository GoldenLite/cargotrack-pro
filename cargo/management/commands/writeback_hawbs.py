"""Принудительный writeback всех колонок «Экспортной статистики» (или
«Общего») для списка HAWB."""
from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='+')

    def handle(self, *args, **opts):
        from cargo.services.sheets.writeback import (
            batch_write_declarations_for_hawbs,
            batch_write_release_dates_for_hawbs,
            batch_write_filed_dates_for_hawbs,
            batch_write_ed_status_for_hawbs,
            batch_write_customs_requests_for_hawbs,
            batch_write_customs_requests_count_for_hawbs,
        )

        hawbs = list(HouseWaybill.objects.filter(
            hawb_number__in=opts['hawbs']))
        if not hawbs:
            self.stdout.write('no hawbs found')
            return
        self.stdout.write(f'HAWB found: {len(hawbs)}')

        n = batch_write_declarations_for_hawbs(hawbs)
        self.stdout.write(f'  decl: {n}')
        n = batch_write_release_dates_for_hawbs(hawbs)
        self.stdout.write(f'  release_date: {n}')
        n = batch_write_filed_dates_for_hawbs(hawbs)
        self.stdout.write(f'  filed_date: {n}')
        n = batch_write_ed_status_for_hawbs(hawbs)
        self.stdout.write(f'  ed_status: {n}')
        n = batch_write_customs_requests_for_hawbs(hawbs)
        self.stdout.write(f'  customs_requests: {n}')
        n = batch_write_customs_requests_count_for_hawbs(hawbs)
        self.stdout.write(f'  customs_requests_count: {n}')
