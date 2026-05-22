"""Batch-reconciliation: для каждой HAWB без ДТ проверить, есть ли в её
Cargo release-сообщение с её hawb_number в raw_xml. Если да — записать.

Это финальный проход после rematch_alta_inbox --all для случаев когда
multi-waybill propagation в dispatch не успела обновить все HAWB-ы.
Например, если HAWB была запромоучена в БД ПОСЛЕ того как release-сообщение
было получено и dispatch'ed.

Запуск:
    uv run python manage.py propagate_missing_declarations --dry-run
    uv run python manage.py propagate_missing_declarations
    uv run python manage.py propagate_missing_declarations --writeback   # + Sheets
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db import connection, OperationalError
from django.db.models import Q

from cargo.models import AltaInboxMessage, HouseWaybill
from cargo.services.alta.inbox import recompute_declaration


class Command(BaseCommand):
    help = 'Достать ДТ из release-сообщений Cargo для HAWB-ов где её нет'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--writeback', action='store_true',
                            help='Также записать в Sheets')
        parser.add_argument('--cargo', default='', help='Только указанная Cargo')

    def handle(self, *args, **opts):
        if connection.vendor == 'sqlite':
            with connection.cursor() as c:
                c.execute('PRAGMA busy_timeout=60000;')

        # HAWB-ы без ДТ, в Cargo
        qs = HouseWaybill.objects.filter(
            customs_declaration_number='',
            mawb__isnull=False,
        )
        if opts['cargo']:
            qs = qs.filter(mawb__awb_number__iexact=opts['cargo'])

        total = qs.count()
        self.stdout.write(f'HAWB без ДТ в Cargo: {total}')

        # Кеш: для каждой Cargo посмотрим есть ли вообще released-сообщения
        # (если нет — пропускать всю партию).
        cargos_with_release = set(
            AltaInboxMessage.objects
            .filter(msg_kind__in=('released', 'withdrawn'),
                    cargo__isnull=False)
            .values_list('cargo_id', flat=True)
            .distinct()
        )
        self.stdout.write(f'Cargo с released/withdrawn-сообщениями: {len(cargos_with_release)}')

        candidates: list[HouseWaybill] = []
        for h in qs.iterator():
            if h.mawb_id not in cargos_with_release:
                continue
            # Проверяем: есть ли в Cargo release-сообщение с h.hawb_number в raw_xml
            found = AltaInboxMessage.objects.filter(
                cargo_id=h.mawb_id,
                msg_kind__in=('released', 'withdrawn'),
                raw_xml__icontains=h.hawb_number,
            ).exists()
            if found:
                candidates.append(h)

        self.stdout.write(self.style.WARNING(
            f'\nКандидатов на propagation: {len(candidates)}'))
        if not candidates:
            return

        # Группируем по Cargo для красивого вывода
        by_cargo: dict[int, list[HouseWaybill]] = {}
        for h in candidates:
            by_cargo.setdefault(h.mawb_id, []).append(h)
        for cargo_id, hawbs in by_cargo.items():
            cargo_num = hawbs[0].mawb.awb_number if hawbs else cargo_id
            self.stdout.write(f'  Cargo {cargo_num}: {len(hawbs)} HAWB-ов')

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING('\nDRY RUN — ничего не записано'))
            return

        # Применяем recompute к каждой
        applied = 0
        errors = 0
        for h in candidates:
            for attempt in range(3):
                try:
                    updated = recompute_declaration(h.mawb, h)
                    if updated:
                        applied += 1
                    break
                except OperationalError as e:
                    if 'locked' in str(e).lower() and attempt < 2:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    errors += 1
                    break
                except Exception as e:
                    errors += 1
                    if errors < 10:
                        self.stdout.write(f'  ERR HAWB {h.hawb_number}: {e}')
                    break

        self.stdout.write(self.style.SUCCESS(
            f'\nПрименено recompute → ДТ записана у {applied} HAWB-ов, errors={errors}'))

        if opts['writeback']:
            self.stdout.write('Sheets writeback...')
            try:
                from cargo.services.sheets.writeback import write_declaration
            except ImportError:
                self.stdout.write('  (writeback недоступен)')
                return
            wb = 0
            for h in candidates:
                h.refresh_from_db(fields=['customs_declaration_number'])
                if not h.customs_declaration_number:
                    continue
                try:
                    write_declaration(h)
                    wb += 1
                except Exception as e:
                    if wb < 10:
                        self.stdout.write(f'  writeback {h.hawb_number}: {e}')
            self.stdout.write(self.style.SUCCESS(f'Sheets writeback: {wb}'))
