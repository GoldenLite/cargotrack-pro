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

from cargo.models import ImportedSheetRow
from cargo.services.sheets.promote import promote_row


class Command(BaseCommand):
    help = 'Промоутит все orphan-строки «Общее» в HAWB + автосоздаёт Cargo'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0,
                            help='Сколько максимум (0 = все)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Только показать сколько будет промоутнуто, без изменений')

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

        promoted = 0
        errors = 0
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

        self.stdout.write(self.style.SUCCESS(
            f'Done. processed={total}, promoted={promoted}, errors={errors}'
        ))
