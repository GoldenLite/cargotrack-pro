"""Bootstrap Cargo records из уникальных ТСД значений в ImportedSheetRow.

Используется один раз после добавления outbox observation, чтобы создать
Cargo для всех партий что уже есть в Sheets «Общее» но ещё не в БД.
После создания — допривязывает накопленные AltaOutboxObservation.

Запуск:
    uv run python manage.py ensure_cargos_from_sheets
    uv run python manage.py ensure_cargos_from_sheets --dry-run
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from cargo.models import AltaOutboxObservation, Cargo, ImportedSheetRow
from cargo.services.alta.outbox import relink_for_cargo
from cargo.services.sheets.mapping import GEN_TSD
from cargo.services.sheets.transport import guess_transport_mode


class Command(BaseCommand):
    help = 'Создаёт Cargo для уникальных ТСД из Sheets «Общее» + допривязывает outbox-наблюдения'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Только вывести что будет создано, без изменений в БД')

    def handle(self, *args, **opts):
        dry = opts['dry_run']

        # Собираем уникальные ТСД из импортированных строк
        tsd_values = set()
        for data in ImportedSheetRow.objects.filter(source__kind='general').values_list('data', flat=True):
            v = (data or {}).get(GEN_TSD)
            if v and isinstance(v, str):
                tsd_values.add(v.strip())
        tsd_values.discard('')

        existing = set(c.lower() for c in Cargo.objects.values_list('awb_number', flat=True))
        to_create = sorted(t for t in tsd_values if t.lower() not in existing)

        self.stdout.write(f'Found {len(tsd_values)} unique ТСД in Sheets')
        self.stdout.write(f'Already in Cargo:    {len(tsd_values) - len(to_create)}')
        self.stdout.write(f'To create:           {len(to_create)}')

        if not to_create:
            self.stdout.write(self.style.SUCCESS('Nothing to do.'))
            return

        if dry:
            for n in to_create:
                mode = guess_transport_mode(n)
                self.stdout.write(f'  WOULD CREATE: {n}  (mode={mode})')
            return

        created = 0
        relinked_total = 0
        with transaction.atomic():
            for n in to_create:
                cargo = Cargo.objects.create(
                    awb_number=n,
                    transportation_mode=guess_transport_mode(n),
                    stage='DRAFT',
                    is_draft=True,
                )
                created += 1
                # Допривязать observations с этим CommonWayBillNumber
                relinked = relink_for_cargo(cargo)
                relinked_total += relinked
                if relinked:
                    self.stdout.write(f'  + {n}  (mode={cargo.transportation_mode}) → relinked {relinked} observations')
                else:
                    self.stdout.write(f'  + {n}  (mode={cargo.transportation_mode})')

        self.stdout.write(self.style.SUCCESS(
            f'Created {created} Cargo, relinked {relinked_total} outbox observations'
        ))
