"""Одноразовый fetch одной партии с moscow-cargo.com (debug/manual).

Использовать для отладки + ручной проверки партии без триггеров cron.

Запуск:
    uv run python manage.py fetch_moscow_cargo --awb 784-84071816
    uv run python manage.py fetch_moscow_cargo --awb 784-84071816 --apply
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from cargo.models import Cargo
from cargo.services.external_warehouse.applier import apply_to_cargo
from cargo.services.external_warehouse.moscow_cargo import MoscowCargoClient


class Command(BaseCommand):
    help = 'Запрос ОДНОЙ партии с moscow-cargo.com (debug)'

    def add_arguments(self, parser):
        parser.add_argument('--awb', required=True, help='Номер AWB, например 784-84071816')
        parser.add_argument('--apply', action='store_true',
                            help='Применить полученные данные к Cargo (если есть) + writeback в Sheets')
        parser.add_argument('--raw', action='store_true',
                            help='Дополнительно вывести полный JSON ответа')

    def handle(self, *args, **opts):
        awb = opts['awb'].strip()

        with MoscowCargoClient() as c:
            if opts['raw']:
                raw = c.fetch_raw(awb)
                self.stdout.write('--- raw JSON ---')
                self.stdout.write(json.dumps(raw, ensure_ascii=False, indent=2))
                self.stdout.write('')
            parsed = c.fetch(awb)

        if not parsed:
            self.stdout.write(self.style.WARNING('Нет данных на сайте (или ДО1 ещё не подан)'))
            return

        self.stdout.write('--- parsed ---')
        for k, v in parsed.items():
            if k in ('awb_info', 'flight'):
                self.stdout.write(f'  {k}: {v}')
                continue
            self.stdout.write(f'  {k:<24} = {v!r}')

        if not opts['apply']:
            self.stdout.write(self.style.NOTICE('--apply не указан, в БД ничего не пишем'))
            return

        cargo = Cargo.objects.filter(awb_number__iexact=awb).first()
        if not cargo:
            raise CommandError(f'Cargo {awb} не найдена в БД (сначала import_sheets + promote)')

        changed = apply_to_cargo(cargo, parsed)
        if changed:
            self.stdout.write(self.style.SUCCESS(f'Cargo {awb} обновлена + sheets writeback'))
        else:
            self.stdout.write('Cargo уже заполнена, ничего не изменили')
