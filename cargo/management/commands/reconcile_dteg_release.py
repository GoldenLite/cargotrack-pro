"""Восстановление release_date / customs_status для ДТЭГ/ПТДЭГ из
собственных consignment-блоков CMN-сообщений.

ДТЭГ/ПТДЭГ имеют per-HAWB решения таможни. Для каждой HAWB СВОЁ решение
(DecisionCode):
    10 — выпуск (released)
    90 — отказ (rejected)
    70 — продление (examination)
    40 — отзыв (withdrawn)
    другое → info

Logic:
1. Берём все HAWB с declaration_form ∈ ('ДТЭГ', 'ПТДЭГ')
2. Для каждой ищем её собственные inbox CMN-сообщения с msg.hawb=h и
   msg_kind ∈ ('released', 'rejected', 'examination', 'withdrawn')
3. Берём самое позднее (по prepared_at) — это "финальный" статус таможни
4. Перезаписываем поля HAWB в соответствии:
   - released → release_date=prepared_at, customs_status='RELEASED'
   - rejected → release_date=None, customs_status='REJECTED'
   - examination/hold → release_date=None, customs_status='EXAMINATION'
   - withdrawn → release_date=None, customs_status='' (отзыв)
   - НЕТ сообщений → release_date=None, customs_status='' (не было решения)
5. Sheets writeback

Идемпотент.

Usage:
    manage.py reconcile_dteg_release --dry-run
    manage.py reconcile_dteg_release                  # реально
    manage.py reconcile_dteg_release --skip-writeback # только БД
"""
from __future__ import annotations

import logging
from collections import Counter

from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill, AltaInboxMessage


logger = logging.getLogger('cargo.reconcile.dteg_release')


# kind → (target customs_status, нужно ли проставить release_date)
KIND_TO_STATUS = {
    'released':    ('RELEASED', True),
    'rejected':    ('REJECTED', False),
    'examination': ('EXAMINATION', False),
    'hold':        ('EXAMINATION', False),
    'withdrawn':   ('', False),
}


class Command(BaseCommand):
    help = 'Восстановление release_date/customs_status для ДТЭГ/ПТДЭГ из per-HAWB сообщений'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--skip-writeback', action='store_true')
        parser.add_argument('--limit', type=int, default=0)

    def handle(self, *args, **opts):
        qs = HouseWaybill.objects.filter(
            declaration_form__in=('ДТЭГ', 'ПТДЭГ'),
        ).order_by('id')
        if opts['limit']:
            qs = qs[:opts['limit']]
        total = qs.count()
        self.stdout.write(f'ДТЭГ/ПТДЭГ HAWB к проверке: {total}')

        changes_counter = Counter()
        to_apply = []  # [(h, kind|None, prepared_at|None)]

        for h in qs.iterator(chunk_size=300):
            # Берём ПОСЛЕДНЕЕ финальное сообщение по prepared_at
            last_msg = AltaInboxMessage.objects.filter(
                hawb=h,
                msg_kind__in=('released', 'rejected', 'examination',
                              'hold', 'withdrawn'),
            ).order_by('-prepared_at').first()

            if last_msg is None:
                target_status = ''
                target_release = None
                desired_kind = None
            else:
                desired_kind = last_msg.msg_kind
                target_status, set_release = KIND_TO_STATUS[desired_kind]
                target_release = last_msg.prepared_at if set_release else None

            # Diff с текущим
            cur_release = h.release_date
            cur_status = (h.customs_status or '')
            need_update = (
                cur_release != target_release or
                cur_status != target_status
            )
            if not need_update:
                continue

            changes_counter[desired_kind or 'no_msg'] += 1
            to_apply.append((h, desired_kind, target_release, target_status))

        self.stdout.write(
            f'Нуждаются в обновлении: {len(to_apply)} HAWB')
        for k, n in changes_counter.most_common():
            self.stdout.write(f'  {k}: {n}')

        if opts['dry_run'] or not to_apply:
            for h, k, r, s in to_apply[:15]:
                cur_r = f'{h.release_date:%d.%m %H:%M}' if h.release_date else 'None'
                new_r = f'{r:%d.%m %H:%M}' if r else 'None'
                self.stdout.write(
                    f'  HAWB {h.hawb_number} ({h.declaration_form}) '
                    f'release: {cur_r}→{new_r}, '
                    f'status: {h.customs_status!r}→{s!r}, kind={k!r}')
            if opts['dry_run']:
                self.stdout.write('DRY RUN — БД не изменена.')
            return

        # Применяем
        applied = []
        for h, kind, target_release, target_status in to_apply:
            new_log_status = h.logistics_status
            if kind == 'released':
                new_log_status = ('IN_TRANSIT_EXP'
                                  if (h.shipment_type or 'IMPORT') == 'EXPORT'
                                  else 'READY_DELIVERY')
            elif kind in (None, 'withdrawn'):
                # Откат на pre-RELEASED состояние
                if h.logistics_status in ('IN_TRANSIT_EXP', 'READY_DELIVERY'):
                    new_log_status = ('EXPORT_CUSTOMS'
                                      if (h.shipment_type or 'IMPORT') == 'EXPORT'
                                      else 'IMPORT_CUSTOMS')
            HouseWaybill.objects.filter(pk=h.pk).update(
                release_date=target_release,
                customs_status=target_status,
                logistics_status=new_log_status,
            )
            applied.append(h)

        self.stdout.write(f'\nОбновлено: {len(applied)} HAWB')

        if opts['skip_writeback']:
            return

        try:
            from cargo.services.sheets.writeback import (
                batch_write_release_dates_for_hawbs,
                batch_write_ed_status_for_hawbs,
            )
            for h in applied:
                h.refresh_from_db()
            self.stdout.write(f'Sheets writeback ({len(applied)} HAWB)...')
            batch_write_release_dates_for_hawbs(applied)
            batch_write_ed_status_for_hawbs(applied)
            try:
                from cargo.services.sheets.crm_realtime import batch_write_all_for_crm_hawbs
                batch_write_all_for_crm_hawbs(applied)
            except Exception:
                logger.exception('crm_realtime skipped')
        except Exception:
            logger.exception('writeback failed')
            self.stdout.write(self.style.ERROR('Writeback exception (см. log)'))
        self.stdout.write(self.style.SUCCESS('Reconcile done.'))
