"""Массово промоутит все orphan-строки из Sheets «Общее» в HAWB.

При промоуте автоматически создаются (если ещё нет):
- Cargo по ТСД (с угаданным transport_mode)
- HAWB с привязкой к этому Cargo
- Допривязывает накопленные AltaOutboxObservation

Используется как разовый бутстрап после `ensure_cargos_from_sheets`,
либо периодически (если включить в Task Scheduler).

Запуск:
    uv run python manage.py promote_all_orphans
    uv run python manage.py promote_all_orphans --limit 500
    uv run python manage.py promote_all_orphans --dry-run
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models.signals import post_save

from cargo.models import HouseWaybill, Cargo, ImportedSheetRow
from cargo.services.sheets.promote import promote_row


class Command(BaseCommand):
    help = 'Промоутит все orphan-строки «Общее» в HAWB + автосоздаёт Cargo'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0,
                            help='Сколько максимум (0 = все)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Только показать сколько будет промоутнуто, без изменений')
        parser.add_argument('--with-workflow', action='store_true',
                            help='Не отключать workflow signals (по умолчанию для bulk отключены)')

    def handle(self, *args, **opts):
        qs = ImportedSheetRow.objects.filter(
            source__kind='general',
            match_status='orphan',
        )
        # Без hawb_number_norm promote бросит ValueError
        qs = qs.exclude(hawb_number_norm='')
        if opts['limit']:
            qs = qs[:opts['limit']]
        total = qs.count()
        self.stdout.write(f'Orphan rows ready to promote: {total}')
        if not total:
            return
        if opts['dry_run']:
            for r in qs[:30]:
                self.stdout.write(f'  WOULD promote: {r.hawb_number_norm}  (ТСД={r.data.get("ТСД", "")})')
            return

        # На время bulk поднимаем busy_timeout — много фоновых сейв могут писать
        # параллельно (workflow_runner, sheets writeback), даже с WAL.
        if connection.vendor == 'sqlite':
            with connection.cursor() as c:
                c.execute('PRAGMA busy_timeout=60000;')

        # По умолчанию отключаем workflow signals — каждый создаваемый HAWB
        # запускает поток workflow_runner с записью в БД. На 12K HAWB подряд
        # это укладывает SQLite. Восстановим после.
        disconnected = []
        if not opts['with_workflow']:
            for sender, uid in [(Cargo, 'cargo_created_workflow'),
                                (HouseWaybill, 'hawb_created_workflow')]:
                ok = post_save.disconnect(sender=sender, dispatch_uid=uid)
                if ok:
                    disconnected.append((sender, uid))
                    self.stdout.write(f'  ⚙ workflow signal disconnected for {sender.__name__}')

        promoted = 0
        errors = 0
        try:
            for i, row in enumerate(qs.iterator(), 1):
                try:
                    promote_row(row, user=None)
                    promoted += 1
                except Exception as e:
                    errors += 1
                    if errors < 10:
                        self.stdout.write(f'  ERR row {row.pk}: {e}')
                if i % 200 == 0:
                    self.stdout.write(f'  progress: {i}/{total}  promoted={promoted} errors={errors}')
        finally:
            # Восстановить сигналы даже если упали
            for sender, uid in disconnected:
                from cargo.apps import CargoConfig  # noqa: F401
                # Сигналы переподключатся при следующем перезапуске процесса;
                # для текущей сессии — повторно вызвать ready() небезопасно.
                # На практике bulk-команда отрабатывает разово и выходит.
                pass

        self.stdout.write(self.style.SUCCESS(
            f'Done. processed={total}, promoted={promoted}, errors={errors}'
        ))
