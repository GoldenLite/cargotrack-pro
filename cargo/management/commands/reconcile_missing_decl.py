"""Sweeper: проставляет customs_declaration_number выпущенным HAWB, у
которых он пуст, хотя inbox несёт собираемый номер.

Класс «registered applied=False» (09-10.07.2026): извещение о регистрации
ДТ (CMN.11337/11001) приходит, но dispatch падает на 'database is locked'
под cron-конкуренцией → applied=False → номер декларации НЕ проставлен.
Позже приходит выпуск (CMN.11350) и применяется (customs_status=RELEASED),
но номер ДТ так и остаётся пустым. В «Общее»/CRM получается «Выпуск
разрешен» без номера ДТ. StuckFinals ловит только финалы (released/
rejected/withdrawn), незастрявшую регистрацию — нет.

Этот sweeper находит RELEASED-без-decl, собирает номер из последнего
значимого inbox-сообщения (_build_declaration_number) и проставляет
прямым UPDATE (recompute_declaration тяжёлый — siblings-транзакция висит
на локе; здесь одна быстрая запись с _retry_on_locked). Листы догоняет
writeback декларации + audit.

Идемпотентно, durable — под cron:
    manage.py reconcile_missing_decl              # dry-run
    manage.py reconcile_missing_decl --apply
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from cargo.models import HouseWaybill, AltaInboxMessage

SIGNIFICANT_EXCLUDE = ('info', 'svh_placed', 'svh_do1_registered',
                       'svh_do2_registered', 'customs_request')


class Command(BaseCommand):
    help = 'Проставляет номер ДТ выпущенным HAWB, где он пуст, но inbox несёт номер.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Реально проставить (без флага — dry-run)')

    def handle(self, *args, **opts):
        from cargo.services.alta.inbox import (_build_declaration_number,
                                               _retry_on_locked)

        targets = list(HouseWaybill.objects
                       .filter(customs_status='RELEASED',
                               customs_declaration_number='')
                       .select_related('mawb'))

        plan = []
        for h in targets:
            cond = Q(hawb=h)
            if h.mawb_id and h.hawb_number:
                cond |= (Q(raw_xml__icontains=h.hawb_number)
                         & Q(cargo=h.mawb))
            msgs = (AltaInboxMessage.objects.filter(cond)
                    .exclude(msg_kind__in=SIGNIFICANT_EXCLUDE)
                    .order_by('-prepared_at', '-received_at'))
            decl = ''
            for m in msgs:
                decl = _build_declaration_number(m.parsed_meta or {})
                if decl:
                    break
            if decl:
                plan.append((h, decl))

        self.stdout.write(f'RELEASED без номера ДТ (восстановимых): {len(plan)}')
        if not opts['apply']:
            for h, decl in plan[:25]:
                self.stdout.write(f'  {h.hawb_number} → {decl}')
            if plan:
                self.stdout.write('(dry-run — добавь --apply)')
            # attempt-часть тоже в dry-режиме (покажет кандидатов)
            self._reconcile_attempts(opts)
            return

        if plan:
            to_write = []
            for h, decl in plan:
                _retry_on_locked(
                    HouseWaybill.objects.filter(pk=h.pk).update,
                    customs_declaration_number=decl, attempts=15)
                h.customs_declaration_number = decl
                to_write.append(h)

            from cargo.services.sheets.writeback import (
                batch_write_declarations_for_hawbs)
            batch_write_declarations_for_hawbs(to_write)
            self.stdout.write(self.style.SUCCESS(
                f'проставлено {len(to_write)} + writeback'))

        self._reconcile_attempts(opts)

    def _reconcile_attempts(self, opts):
        """Класс 2: RELEASED в БД, но попытка текущей декларации НЕ
        RELEASED (FILED/REJECTED) → compute_ed_status рассинхрон
        ('Присвоен номер'/'Отказано') → номер ДТ не подтягивается в лист
        (колонка W пишется только при 'Выпуск разрешен').

        Причины: (а) lean-применение выпуска (bulk_update customs_status)
        не обновляло attempt; (б) перепутанные attempts multi-HAWB ДТ
        (выпущенная ДТ помечена REJECTED, см. 10275193543); (в) выпуск
        старого агента без release-сообщения в inbox (attempt остался FILED).

        Выравниваем: attempt current-decl → RELEASED, ЕСЛИ выпуск реален
        (release_date есть) и НЕТ более свежего отказа/отзыва (реальный
        отказ ПОСЛЕ выпуска не перетираем). Тяжёлый guard по raw_xml —
        только для невыровненных кандидатов (не для всех RELEASED)."""
        from cargo.models import HawbDeclarationAttempt
        from cargo.services.alta.inbox import _retry_on_locked

        cand = (HouseWaybill.objects
                .filter(customs_status='RELEASED', release_date__isnull=False)
                .exclude(customs_declaration_number='')
                .prefetch_related('declaration_attempts')
                .select_related('mawb'))
        fix = []
        for h in cand:
            cur = (h.customs_declaration_number or '').strip()
            att = next((a for a in h.declaration_attempts.all()
                        if a.declaration_number == cur), None)
            if att is None or att.status == 'RELEASED':
                continue  # нет попытки для current-decl или уже выровнена
            # guard: нет более свежего отказа/отзыва, чем выпуск
            cond = Q(hawb=h)
            if h.mawb_id and h.hawb_number:
                cond |= (Q(raw_xml__icontains=h.hawb_number)
                         & Q(cargo=h.mawb))
            newer_rej = (AltaInboxMessage.objects
                         .filter(cond, prepared_at__gt=h.release_date,
                                 msg_kind__in=('rejected', 'withdrawn'))
                         .exists())
            if newer_rej:
                continue
            fix.append((h, att))

        self.stdout.write(f'RELEASED с невыровненной попыткой: {len(fix)}')
        if not opts['apply'] or not fix:
            if fix and not opts['apply']:
                for h, att in fix[:25]:
                    self.stdout.write(f'  {h.hawb_number} attempt '
                                      f'{att.status}→RELEASED')
            return

        to_write = []
        for h, att in fix:
            _retry_on_locked(
                HawbDeclarationAttempt.objects.filter(pk=att.pk).update,
                status='RELEASED', attempts=15)
            to_write.append(h)

        from cargo.services.sheets.writeback import (
            batch_write_ed_status_for_hawbs,
            batch_write_declarations_for_hawbs)
        batch_write_declarations_for_hawbs(to_write)
        batch_write_ed_status_for_hawbs(to_write)
        self.stdout.write(self.style.SUCCESS(
            f'выровнено попыток {len(to_write)} + writeback'))
