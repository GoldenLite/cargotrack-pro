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

    def handle(self, *args, **opts):
        from cargo.services.alta.inbox import dispatch, _retry_on_locked
        from cargo.services.sheets.writeback import (
            begin_batch_writeback, end_batch_writeback)

        qs = _stuck_qs()
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

        applied = still = err = 0
        # Подавляем per-msg Sheets writeback на весь прогон (анти-шторм).
        begin_batch_writeback()
        try:
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
        finally:
            end_batch_writeback()

        self.stdout.write(self.style.SUCCESS(
            f'применено {applied}, не применилось(guard) {still}, ошибок {err}'))
        self.stdout.write(f'осталось застрявших: {_stuck_qs().count()}')
        self.stdout.write('Sheets догонит audit_sheets_vs_db (15м) + crm_sync.')
