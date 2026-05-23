"""Backfill дат подачи HAWB в Sheets-колонку «CargoTrack: дата подачи».

Аналог resync_release_dates — пишет только HAWB у которых уже заполнен
filed_date (forward-only). Историческое заполнение (по customs_status_date /
created_at) не делается — иначе старым выпускам припишется дата деплоя.

Запуск:
    uv run python manage.py resync_filed_dates
    uv run python manage.py resync_filed_dates --dry-run
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill


class Command(BaseCommand):
    help = 'Backfill filed_date в Sheets для всех HAWB с заполненным filed_date'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Только показать кандидатов, в Sheets не пишем')
        parser.add_argument('--limit', type=int, default=0,
                            help='Лимит обработанных HAWB (для теста)')

    def handle(self, *args, **opts):
        qs = (HouseWaybill.objects
              .filter(filed_date__isnull=False)
              .order_by('hawb_number'))
        if opts['limit']:
            qs = qs[:opts['limit']]

        hawbs = list(qs)
        self.stdout.write(f'Кандидатов: {len(hawbs)} (HAWB с filed_date)')

        if opts['dry_run']:
            for h in hawbs[:30]:
                self.stdout.write(
                    f'  {h.hawb_number}: {h.filed_date.strftime("%d.%m.%Y")}'
                )
            if len(hawbs) > 30:
                self.stdout.write(f'  ... и ещё {len(hawbs) - 30}')
            return

        if not hawbs:
            return

        from cargo.services.sheets.writeback import batch_write_filed_dates_for_hawbs
        cells = batch_write_filed_dates_for_hawbs(hawbs)
        self.stdout.write(self.style.SUCCESS(
            f'Done. cells_written={cells}'
        ))
