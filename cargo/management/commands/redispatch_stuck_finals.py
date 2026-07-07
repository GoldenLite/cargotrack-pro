"""Sweeper: переприменяет ЗАСТРЯВШИЕ финальные сообщения таможни.

Проблема: финальное сообщение (released/rejected/withdrawn) приходит, но
остаётся status_applied=False при заданном hawb_id — «сматчено, но не
применено». Механизм: сообщение прилетело раньше, чем HAWB стала матчиться
→ apply пропущен → позже sweeper/db_reconcile проставил hawb_id, но статус
НЕ переприменил. Планового переприменения таких не было → копятся (на
06.07.2026 накопилось 53) → «выпуск не доходит» до «Общее»/CRM.

Этот sweeper находит их и переприменяет через inbox.dispatch.

АНТИ-ЛОК-ШТОРМ (важно): массовый прямой re-dispatch устраивает лок-шторм
(конкуренция с кронами за SQLite write-lock + пачки Sheets-writeback
потоков). Поэтому здесь:
  - Sheets-writeback ПОДАВЛЕН на время прогона (begin_batch_writeback) —
    dispatch делает только DB-изменения, без Sheets-API;
  - каждый dispatch обёрнут в _retry_on_locked (retry на 'database is
    locked' вместо падения);
  - листы догоняет audit_sheets_vs_db (15 мин, сверяет ed_status/выпуск/
    декларацию из БД) + crm_sync.

Запуск (durable — можно кроном по --limit, чтобы чистить постепенно):
    manage.py redispatch_stuck_finals              # dry-run (показать)
    manage.py redispatch_stuck_finals --apply
    manage.py redispatch_stuck_finals --apply --limit 5

--lean (РЕКОМЕНДУЕТСЯ для крона): вместо dispatch — прямой bulk_update
статуса (мс вместо минут). Проверено 07.07.2026: даже 3 dispatch'а под
вечерним контеншеном не уложились в 25 мин (задачу убил планировщик), а
lean-путь применил 26 выпусков за 2 секунды. Sheets догоняет
audit_sheets_vs_db (CargoTrack-AuditFix). Гарды: newer_final + per-HAWB
DecisionCode из parsed_meta.consignments (msg_kind — ДОМИНАНТНЫЙ kind
сообщения, решение конкретной HAWB может отличаться!).
"""
from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage

FINAL_KINDS = ['released', 'rejected', 'withdrawn']


def _stuck_qs():
    return (AltaInboxMessage.objects
            .filter(status_applied=False, hawb__isnull=False,
                    msg_kind__in=FINAL_KINDS)
            .order_by('prepared_at'))


class Command(BaseCommand):
    help = 'Переприменяет застрявшие финальные сообщения (release/reject/withdraw).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Реально переприменить (без флага — dry-run)')
        parser.add_argument('--limit', type=int, default=0,
                            help='Сколько за прогон (0 = все)')
        parser.add_argument('--lean', action='store_true',
                            help='bulk_update вместо dispatch (быстро, '
                                 'минимум lock-времени; листы догонит audit)')

    def handle(self, *args, **opts):
        from cargo.services.alta.inbox import dispatch, _retry_on_locked
        from cargo.services.sheets.writeback import (
            begin_batch_writeback, end_batch_writeback)

        qs = _stuck_qs().select_related('hawb')
        if opts['limit']:
            qs = qs[:opts['limit']]
        msgs = list(qs)
        self.stdout.write(f'застрявших финалов к обработке: {len(msgs)}')
        if not msgs:
            return
        if not opts['apply']:
            for m in msgs[:25]:
                self.stdout.write(
                    f'  #{m.pk} {m.msg_type} {m.msg_kind} '
                    f'{m.hawb.hawb_number if m.hawb else "?"}')
            self.stdout.write('(dry-run — добавь --apply)')
            return

        if opts['lean']:
            self._lean(msgs)
            return

        applied = still = err = 0
        # Подавляем per-msg Sheets writeback на весь прогон (анти-шторм).
        begin_batch_writeback()
        try:
            self._dispatch_loop(msgs, dispatch, _retry_on_locked)
        finally:
            end_batch_writeback()

    # ─── lean-режим: bulk_update без dispatch ──

    def _lean(self, msgs):
        from django.utils.dateparse import parse_datetime
        from cargo.models import HouseWaybill
        from cargo.services.alta.inbox import (
            DECISION_CODE_KIND, _retry_on_locked)

        to_update = {}   # hawb_pk -> hawb (с выставленными полями)
        mark_ids = []    # message pk -> пометить applied
        skipped = []     # (msg, причина) — оставляем applied=False
        for m in msgs:
            h = m.hawb
            # Гард: есть более свежий финал по этой HAWB → статус пусть
            # определяет он; это сообщение только помечаем applied.
            newer = (AltaInboxMessage.objects
                     .filter(hawb=h, prepared_at__gt=m.prepared_at,
                             msg_kind__in=FINAL_KINDS)
                     .exclude(pk=m.pk).exists())
            if newer:
                mark_ids.append(m.pk)
                continue
            # Per-HAWB решение: msg_kind — доминантный kind ВСЕГО сообщения,
            # у конкретной HAWB в consignment-блоке может быть другой код.
            kind = m.msg_kind
            cons_dt = None
            for c in (m.parsed_meta or {}).get('consignments') or []:
                wbs = [str(w).upper() for w in (c.get('waybills') or [])]
                if (h.hawb_number or '').upper() in wbs:
                    dc = (c.get('decision_code') or '').strip()
                    kind = DECISION_CODE_KIND.get(dc, kind)
                    cons_dt = c.get('decision_date')
                    break
            if kind == 'released':
                if h.customs_status != 'RELEASED':
                    h.customs_status = 'RELEASED'
                    h.release_date = ((parse_datetime(cons_dt)
                                       if cons_dt else None) or m.prepared_at)
                    to_update[h.pk] = h
                mark_ids.append(m.pk)
            elif kind == 'rejected':
                if h.customs_status != 'REJECTED':
                    h.customs_status = 'REJECTED'
                    h.release_date = None
                    to_update[h.pk] = h
                mark_ids.append(m.pk)
            elif kind == 'withdrawn':
                # Статус не трогаем (семантика отзыва сложнее — dispatch);
                # только помечаем, чтобы не висело в backlog'е.
                mark_ids.append(m.pk)
            else:
                # per-HAWB решение не финальное (hold/examination/info) —
                # финал этой HAWB ещё впереди, сообщение просто помечаем.
                mark_ids.append(m.pk)
                skipped.append((m, f'per-HAWB kind={kind}'))

        if to_update:
            _retry_on_locked(
                HouseWaybill.objects.bulk_update, list(to_update.values()),
                ['customs_status', 'release_date'], batch_size=100)
        if mark_ids:
            _retry_on_locked(
                AltaInboxMessage.objects.filter(pk__in=mark_ids).update,
                status_applied=True)
        for m, why in skipped:
            self.stdout.write(f'  #{m.pk} {m.hawb.hawb_number}: статус не '
                              f'менялся ({why}), помечено applied')
        self.stdout.write(self.style.SUCCESS(
            f'lean: статус обновлён у {len(to_update)} HAWB, '
            f'помечено applied {len(mark_ids)}'))
        self.stdout.write(f'осталось застрявших: {_stuck_qs().count()}')
        self.stdout.write('Sheets догонит audit_sheets_vs_db (AuditFix).')

    def _dispatch_loop(self, msgs, dispatch, _retry_on_locked):
        applied = still = err = 0
        for m in msgs:
            try:
                _retry_on_locked(dispatch, m)
                m.refresh_from_db(fields=['status_applied'])
                if m.status_applied:
                    applied += 1
                else:
                    still += 1
            except Exception as e:  # noqa: BLE001
                err += 1
                self.stderr.write(f'  #{m.pk} {getattr(m.hawb, "hawb_number", "?")}: {e}')

        self.stdout.write(self.style.SUCCESS(
            f'применено {applied}, не применилось(guard) {still}, ошибок {err}'))
        self.stdout.write(f'осталось застрявших: {_stuck_qs().count()}')
        self.stdout.write('Sheets догонит audit_sheets_vs_db (15м) + crm_sync.')
