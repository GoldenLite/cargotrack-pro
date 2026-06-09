"""Откат ошибочной пропагации release_date на siblings ДТЭГ/ПТДЭГ.

ДТЭГ/ПТДЭГ — это per-HAWB решения таможни (одна декларация, но по каждой
накладной отдельное решение: одна выпущена, другая отказ, третья на досмотр).
Пропагация одного release_date на всех siblings — ОШИБКА.

Эта команда:
1. Находит HAWB с declaration_form ∈ ('ДТЭГ', 'ПТДЭГ') и release_date IS NOT NULL
2. Для каждой проверяет: есть ли её СОБСТВЕННЫЙ inbox CMN.11350 (kind='released',
   hawb=self) или CMN.11341/11314 (ЭК-release)
3. Если нет → release_date был пропагирован ошибочно → откатываем

Логика:
- release_date = None
- customs_status = '' (вернуть к "в таможне")
- logistics_status = 'EXPORT_CUSTOMS' (вернуть)

Usage:
    manage.py rollback_dteg_propagation --dry-run
    manage.py rollback_dteg_propagation
"""
from __future__ import annotations

import logging
from django.core.management.base import BaseCommand
from cargo.models import HouseWaybill, AltaInboxMessage


logger = logging.getLogger('cargo.rollback.dteg_prop')

RELEASE_TYPES = ('CMN.11350', 'CMN.11341', 'CMN.11314')


class Command(BaseCommand):
    help = 'Откат ошибочной пропагации release_date на siblings ДТЭГ/ПТДЭГ'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--skip-writeback', action='store_true')

    def handle(self, *args, **opts):
        # Кандидаты: ДТЭГ/ПТДЭГ с release_date IS NOT NULL
        qs = HouseWaybill.objects.filter(
            declaration_form__in=('ДТЭГ', 'ПТДЭГ'),
            release_date__isnull=False,
        ).order_by('id')
        self.stdout.write(f'ДТЭГ/ПТДЭГ с release_date: {qs.count()}')

        to_rollback = []
        for h in qs.iterator(chunk_size=200):
            # Есть ли inbox release ИМЕННО для этой HAWB?
            has_own_release = AltaInboxMessage.objects.filter(
                hawb=h, msg_kind='released',
            ).exists()
            if not has_own_release:
                to_rollback.append(h)
        self.stdout.write(f'Без собственного released-сообщения: {len(to_rollback)}')

        if not to_rollback:
            return

        for h in to_rollback[:30]:
            self.stdout.write(
                f'  HAWB {h.hawb_number} ({h.declaration_form}) '
                f'release={h.release_date:%d.%m %H:%M}, '
                f'customs={h.customs_status} → откат')

        if opts['dry_run']:
            self.stdout.write(f'\nDRY RUN — БД не изменена ({len(to_rollback)} ждут отката).')
            return

        rolled = []
        for h in to_rollback:
            HouseWaybill.objects.filter(pk=h.pk).update(
                release_date=None,
                customs_status='',
                logistics_status='EXPORT_CUSTOMS',
            )
            rolled.append(h)

        self.stdout.write(f'\nОткатано: {len(rolled)} HAWB')

        if opts['skip_writeback']:
            return

        try:
            from cargo.services.sheets.writeback import (
                batch_write_release_dates_for_hawbs,
                batch_write_ed_status_for_hawbs,
            )
            self.stdout.write('Sheets writeback (откат): release + ed_status')
            # release_date=None → writeback запишет пустую ячейку
            for h in rolled:
                h.refresh_from_db()
            batch_write_release_dates_for_hawbs(rolled)
            batch_write_ed_status_for_hawbs(rolled)
            try:
                from cargo.services.sheets.crm_realtime import batch_write_all_for_crm_hawbs
                batch_write_all_for_crm_hawbs(rolled)
            except Exception:
                logger.exception('crm_realtime rollback skipped')
        except Exception:
            logger.exception('writeback rollback failed')
        self.stdout.write(self.style.SUCCESS('Rollback done.'))
