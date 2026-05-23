"""Пересинхрон СВХ-колонок в Sheets («Общее»).

После того как inbox разобрал CMN.13029-сообщения и заполнил
Cargo.warehouse_license + scan_into_bond — эта команда выбирает все такие
партии и пишет их в две новые колонки Sheets («CargoTrack: лицензия СВХ»,
«CargoTrack: дата размещения»).

Парная к `resync_sheets_declarations` но для СВХ-данных. Использовать когда:
- впервые включили интеграцию (массовый backfill);
- юзер сортировал/удалял дубли в Sheets без включения СВХ-колонок —
  значения съехали (тот же сценарий что с ДТ);
- свежепромоутнутые HAWB не получили writeback в реальном времени.

Запуск:
    uv run python manage.py resync_sheets_svh
    uv run python manage.py resync_sheets_svh --cargo 222-40333075
    uv run python manage.py resync_sheets_svh --dry-run
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import Cargo


class Command(BaseCommand):
    help = 'Batch-резинк лицензии СВХ + даты размещения в Sheets'

    def add_arguments(self, parser):
        parser.add_argument('--cargo', default='', help='Только указанная партия')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        from django.db.models import Q
        # Партия попадает в выборку если у неё ЛЮБОЕ из СВХ-полей заполнено —
        # лицензия / дата размещения / рег.номер ДО1. Часть полей может
        # отсутствовать у исторических партий, но писать что есть всё равно надо.
        qs = (Cargo.objects
              .filter(Q(warehouse_license__gt='') |
                      Q(scan_into_bond__isnull=False) |
                      Q(svh_do1_reg_number__gt=''))
              .filter(warehouse_license='10001/060324/10009/1'))
        if opts['cargo']:
            qs = qs.filter(awb_number__iexact=opts['cargo'])
        if opts['limit']:
            qs = qs[:opts['limit']]

        cargos = list(qs)
        self.stdout.write(f'Cargo с заполненными СВХ-полями: {len(cargos)}')

        if opts['dry_run']:
            for c in cargos[:30]:
                date_str = c.scan_into_bond.strftime('%d.%m.%Y') if c.scan_into_bond else '—'
                self.stdout.write(
                    f'  {c.awb_number:<22} {c.warehouse_license:<25} '
                    f'{date_str:<12} {c.svh_do1_reg_number}'
                )
            if len(cargos) > 30:
                self.stdout.write(f'  ... и ещё {len(cargos) - 30}')
            return

        from cargo.services.sheets.writeback import write_svh_placement_for_cargo

        total_writes = 0
        for c in cargos:
            try:
                written = write_svh_placement_for_cargo(c)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  {c.awb_number}: {e}'))
                continue
            total_writes += written
            if written:
                self.stdout.write(f'  {c.awb_number}: записано {written} ячеек')

        self.stdout.write(self.style.SUCCESS(
            f'Done. cargos={len(cargos)} cells_written={total_writes}'
        ))
