"""Мягко архивировать HAWB, пропавшие из «Общее» («не прилетела»).

Ручной / тестовый вызов того же кода, что дёргает импортёр при удалении строки
из «Общее». Отвязывает HAWB от партии + чистит CRM + аудит-событие. ОБРАТИМО
(запись HouseWaybill сохраняется). Предохранители — см. services/sheets/archive.py.

    manage.py archive_missing_hawbs 10295708795              # dry-run
    manage.py archive_missing_hawbs 10295708795 --apply
"""
from django.core.management.base import BaseCommand

from cargo.services.sheets.archive import archive_missing_general_hawbs


class Command(BaseCommand):
    help = 'Мягко архивировать HAWB, пропавшие из «Общее» (отвязка + CRM + аудит).'

    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='+', help='HAWB номера через пробел')
        parser.add_argument('--apply', action='store_true',
                            help='Реально архивировать (без флага — dry-run)')

    def handle(self, *args, **opts):
        res = archive_missing_general_hawbs(
            opts['hawbs'], apply=opts['apply'], verify_absent=True)
        for k, v in res.items():
            self.stdout.write(f'  {k}: {v}')
