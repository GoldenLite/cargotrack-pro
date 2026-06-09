"""Backfill: пропагирует release_date / customs_status на sibling HAWB
которые остались с release_date=None из-за бага до фикса apply_status
(см. memory project_release_propagation, обнаружено 09.06.2026).

Логика:
1. Найти HAWB с release_date IS NOT NULL И customs_declaration_number != ''
2. Сгруппировать по customs_declaration_number — это ДТЭГ
3. Для каждой группы взять самую раннюю release_date (одна ДТ — один выпуск)
4. UPDATE siblings (тот же decl, release_date IS NULL):
   - release_date = max_release_date
   - customs_status = 'RELEASED'
   - logistics_status = 'IN_TRANSIT_EXP' (EXPORT) или 'READY_DELIVERY' (IMPORT)
5. Sheets writeback: release_date + ed_status

Идемпотент: уже актуальные siblings пропускает.

Usage:
    manage.py backfill_release_propagation --dry-run     # preview
    manage.py backfill_release_propagation               # реальный прогон
    manage.py backfill_release_propagation --skip-writeback   # только БД
"""
from __future__ import annotations

import logging
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db.models import Min

from cargo.models import HouseWaybill


logger = logging.getLogger('cargo.backfill.release_prop')


class Command(BaseCommand):
    help = 'Backfill: пропагирует release_date на siblings одной ДТЭГ'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Показать сколько HAWB бы обновилось')
        parser.add_argument('--skip-writeback', action='store_true',
                            help='Только БД, без Sheets')
        parser.add_argument('--limit', type=int, default=0,
                            help='Только первые N HAWB (0 = все)')

    def handle(self, *args, **opts):
        # 1. Найти все ДТ у которых есть released HAWB (release_date IS NOT NULL)
        released = (HouseWaybill.objects
                    .exclude(customs_declaration_number='')
                    .exclude(release_date__isnull=True)
                    .values('customs_declaration_number')
                    .annotate(min_release=Min('release_date'))
                    .order_by('-min_release'))
        decl_to_release: dict[str, object] = {
            r['customs_declaration_number']: r['min_release']
            for r in released
        }
        self.stdout.write(f'Найдено ДТ с released HAWB: {len(decl_to_release)}')

        # 2. Найти siblings с теми же decl но release_date IS NULL
        siblings = (HouseWaybill.objects
                    .filter(customs_declaration_number__in=list(decl_to_release.keys()))
                    .filter(release_date__isnull=True)
                    .order_by('id'))
        if opts['limit']:
            siblings = siblings[:opts['limit']]
        sibling_list = list(siblings)
        self.stdout.write(f'Кандидатов на пропагацию (release_date=None, decl есть): {len(sibling_list)}')

        if not sibling_list:
            self.stdout.write('Нечего пропагировать.')
            return

        # 3. Группируем по shipment_type для правильного logistics_status
        by_decl_and_type: dict[tuple, list] = defaultdict(list)
        for h in sibling_list:
            key = (h.customs_declaration_number, (h.shipment_type or 'IMPORT'))
            by_decl_and_type[key].append(h)

        updated_pks = []
        update_count = 0
        for (decl, ship_type), group in by_decl_and_type.items():
            release_dt = decl_to_release[decl]
            new_log_status = ('IN_TRANSIT_EXP' if ship_type == 'EXPORT'
                              else 'READY_DELIVERY')
            for h in group:
                if opts['dry_run']:
                    update_count += 1
                    updated_pks.append(h.pk)
                    if update_count <= 30:
                        self.stdout.write(
                            f'  DRY: HAWB {h.hawb_number} ({ship_type}) '
                            f'← release_date={release_dt:%d.%m %H:%M} '
                            f'logistics={h.logistics_status}→{new_log_status}')
                else:
                    HouseWaybill.objects.filter(pk=h.pk).update(
                        release_date=release_dt,
                        customs_status='RELEASED',
                        logistics_status=new_log_status,
                    )
                    updated_pks.append(h.pk)
                    update_count += 1
                    if update_count % 100 == 0:
                        self.stdout.write(
                            f'  ... {update_count}/{len(sibling_list)} обновлено')

        self.stdout.write(
            f'\nИтого: {update_count} HAWB '
            f'({"DRY" if opts["dry_run"] else "реально"} обновлено)')

        if opts['dry_run'] or opts['skip_writeback'] or not updated_pks:
            return

        self.stdout.write(f'Sheets writeback для {len(updated_pks)} HAWB...')
        hawbs = list(HouseWaybill.objects.filter(pk__in=updated_pks))
        try:
            from cargo.services.sheets.writeback import (
                batch_write_release_dates_for_hawbs,
                batch_write_ed_status_for_hawbs,
            )
            # Разделяем по shipment_type для правильной writeback-вкладки
            export_hawbs = [h for h in hawbs if h.shipment_type == 'EXPORT']
            import_hawbs = [h for h in hawbs if h.shipment_type != 'EXPORT']
            if import_hawbs:
                self.stdout.write(f'  IMPORT ({len(import_hawbs)}): release + ed_status')
                batch_write_release_dates_for_hawbs(import_hawbs)
                batch_write_ed_status_for_hawbs(import_hawbs)
            if export_hawbs:
                self.stdout.write(f'  EXPORT ({len(export_hawbs)}): release + ed_status')
                # batch_write_*_for_hawbs автоматически роутят по shipment_type
                # (export → kind=export, import → kind=general)
                batch_write_release_dates_for_hawbs(export_hawbs)
                batch_write_ed_status_for_hawbs(export_hawbs)
            # CRM realtime — если HAWB есть в CRM
            try:
                from cargo.services.sheets.crm_realtime import batch_write_all_for_crm_hawbs
                batch_write_all_for_crm_hawbs(hawbs)
            except Exception:
                logger.exception('crm_realtime writeback skipped')
        except Exception:
            logger.exception('writeback failed')
            self.stdout.write(self.style.ERROR('Writeback exception (см. log)'))
        self.stdout.write(self.style.SUCCESS('Backfill done.'))
