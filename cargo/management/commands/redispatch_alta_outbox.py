"""Передиспатчить все AltaOutboxObservation — backfill после изменений в outbox.dispatch.

Аналог reparse_alta_inbox, но для исходящих наблюдений (538134^*.gz).
Используется после миграций, которые меняют какие поля заполняет dispatch
(например, 0055: svh_do1_sent_at Cargo→HouseWaybill — миграция не переносит
данные, нужен передиспатч для восстановления).

Запуск:
    uv run python manage.py redispatch_alta_outbox                  # все
    uv run python manage.py redispatch_alta_outbox --type ED.DO1    # только ED.DO1
    uv run python manage.py redispatch_alta_outbox --dry-run
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db import connection, OperationalError

from cargo.models import AltaOutboxObservation
from cargo.services.alta.outbox import dispatch


class Command(BaseCommand):
    help = 'Передиспатчить AltaOutboxObservation (backfill после миграций outbox.dispatch)'

    def add_arguments(self, parser):
        parser.add_argument('--type', dest='msg_type', default='',
                            help='Только указанный msg_type (например ED.DO1)')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        if connection.vendor == 'sqlite':
            with connection.cursor() as c:
                c.execute('PRAGMA busy_timeout=120000;')

        qs = AltaOutboxObservation.objects.all().order_by('prepared_at')
        if opts['msg_type']:
            qs = qs.filter(msg_type=opts['msg_type'])
        if opts['limit']:
            qs = qs[:opts['limit']]

        pks = list(qs.values_list('pk', flat=True))
        self.stdout.write(f'Redispatch: {len(pks)} observations, '
                          f'dry_run={opts["dry_run"]}')

        # Подавляем per-вызов sheets writeback — в конце один resync.
        from cargo.services.sheets.writeback import (
            begin_batch_writeback, end_batch_writeback,
        )
        if not opts['dry_run']:
            begin_batch_writeback()

        errors = 0
        BACKOFF = [1, 2, 4, 8, 16, 32]
        for i, pk in enumerate(pks, 1):
            for attempt in range(len(BACKOFF) + 1):
                try:
                    obs = AltaOutboxObservation.objects.get(pk=pk)
                    if not opts['dry_run']:
                        dispatch(obs)
                    break
                except OperationalError as e:
                    if 'locked' in str(e).lower() and attempt < len(BACKOFF):
                        time.sleep(BACKOFF[attempt])
                        continue
                    errors += 1
                    if errors < 10:
                        self.stdout.write(f'  ERR #{pk}: {e}')
                    break
                except Exception as e:
                    errors += 1
                    if errors < 10:
                        import traceback
                        self.stdout.write(f'  ERR #{pk} ({obs.msg_type if "obs" in dir() else "?"}): {e}')
                        self.stdout.write(traceback.format_exc())
                    break
            if i % 200 == 0:
                self.stdout.write(f'  progress: {i}/{len(pks)} errors={errors}')

        if not opts['dry_run']:
            end_batch_writeback()

            # Sheets resync после bulk — пишем только per-HAWB svh_do1_sent_at,
            # weight/places, и cargo-level svh (лицензия + scan_into_bond + рег.№ ДО1).
            self.stdout.write('')
            self.stdout.write(self.style.NOTICE('Sheets resync...'))
            try:
                from cargo.models import HouseWaybill, Cargo, ImportedSheetRow
                from cargo.services.sheets.writeback import (
                    batch_write_svh_for_cargos,
                    batch_write_svh_do1_sent_for_hawbs,
                    batch_write_svh_do1_weight_for_hawbs,
                    batch_write_svh_do1_places_for_hawbs,
                )
                hawb_nums_in_sheets = list(
                    ImportedSheetRow.objects.filter(source__kind='general')
                    .values_list('hawb_number_norm', flat=True).distinct()
                )
                hawbs_for_do1 = list(HouseWaybill.objects.filter(
                    hawb_number__in=hawb_nums_in_sheets
                ).distinct())
                if hawbs_for_do1:
                    n = batch_write_svh_do1_sent_for_hawbs(hawbs_for_do1)
                    self.stdout.write(f'  svh_do1_sent: {n} cells ({len(hawbs_for_do1)} HAWB)')
                    n = batch_write_svh_do1_weight_for_hawbs(hawbs_for_do1)
                    self.stdout.write(f'  svh_do1_weight: {n} cells')
                    n = batch_write_svh_do1_places_for_hawbs(hawbs_for_do1)
                    self.stdout.write(f'  svh_do1_places: {n} cells')
                cargos_svh = list(Cargo.objects.filter(hawbs__isnull=False).distinct())
                if cargos_svh:
                    n = batch_write_svh_for_cargos(cargos_svh)
                    self.stdout.write(f'  svh: {n} cells ({len(cargos_svh)} cargos)')
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Sheets resync failed: {e}'))

        self.stdout.write(self.style.SUCCESS(
            f'Done. processed={len(pks)} errors={errors}'))
