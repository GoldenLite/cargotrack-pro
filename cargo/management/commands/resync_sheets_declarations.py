"""Принудительный writeback: пройти по всем HAWB у которых в БД стоит
customs_declaration_number и записать его в Google Sheets «CargoTrack: ДТ».

Полезно когда:
- ДТ в БД есть, но в Sheets пробелы (writeback падал на 429 rate-limit,
  Google API не успел в момент массового rematch).
- Поменялся formatting/spec колонки и нужна полная синхронизация.

write_declaration() уже идемпотент: читает текущее значение и пропускает
если совпадает. Поэтому повторный прогон безопасен.

Запуск:
    uv run python manage.py resync_sheets_declarations             # все с ДТ
    uv run python manage.py resync_sheets_declarations --cargo 190526-2
    uv run python manage.py resync_sheets_declarations --limit 100
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill
from cargo.services.sheets.writeback import write_declaration


class Command(BaseCommand):
    help = 'Принудительно записать customs_declaration_number в Sheets для всех HAWB с ДТ'

    def add_arguments(self, parser):
        parser.add_argument('--cargo', default='',
                            help='Только указанная Cargo (awb_number)')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--throttle-ms', type=int, default=100,
                            help='Пауза между записями (мс), чтобы не упереться в 429')

    def handle(self, *args, **opts):
        qs = HouseWaybill.objects.exclude(customs_declaration_number='')
        if opts['cargo']:
            qs = qs.filter(mawb__awb_number__iexact=opts['cargo'])
        if opts['limit']:
            qs = qs[:opts['limit']]

        pks = list(qs.values_list('pk', flat=True))
        self.stdout.write(f'Resync writeback: {len(pks)} HAWB с заполненной ДТ')

        sleep_s = max(0.0, opts['throttle_ms'] / 1000.0)
        wrote = 0
        skipped = 0
        errors = 0
        for i, pk in enumerate(pks, 1):
            try:
                h = HouseWaybill.objects.only('pk', 'hawb_number',
                                               'customs_declaration_number',
                                               'mawb_id').get(pk=pk)
                ok = write_declaration(h)
                if ok:
                    wrote += 1
                else:
                    skipped += 1
            except Exception as e:
                errors += 1
                if errors < 10:
                    self.stdout.write(f'  ERR pk={pk}: {e}')
            if sleep_s:
                time.sleep(sleep_s)
            if i % 50 == 0:
                self.stdout.write(
                    f'  progress: {i}/{len(pks)} wrote={wrote} skipped={skipped} errors={errors}')

        self.stdout.write(self.style.SUCCESS(
            f'Done. processed={len(pks)} wrote={wrote} skipped={skipped} errors={errors}'
        ))
