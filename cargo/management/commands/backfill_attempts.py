"""Backfill HawbDeclarationAttempt по текущим customs_declaration_number.

Для каждой HouseWaybill у которой customs_declaration_number не пуст —
создаёт attempt #1 (если ещё нет). Это **минимальный** backfill: история
переподач, которые были до 2026-05-28, не восстанавливается (для этого
нужен анализ AltaInboxMessage всех CMN.11350 с разными ДТ для одного
HAWB — отдельная задача).

После создания attempts — синхронно пишет колонку «Переподачи» в Sheets.

Запуск:
    python manage.py backfill_attempts
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import HawbDeclarationAttempt, HouseWaybill
from cargo.services.alta.inbox import _register_attempt


class Command(BaseCommand):
    help = 'Создать attempt #1 для HAWB с customs_declaration_number'

    def handle(self, *args, **opts):
        qs = HouseWaybill.objects.exclude(
            customs_declaration_number='').only(
            'pk', 'hawb_number', 'customs_declaration_number',
            'filed_date', 'release_date',
        )
        self.stdout.write(f'HAWB с customs_declaration_number: {qs.count()}')

        # Загружаем всё в память сразу — итератор держит SQLite-курсор и
        # ловит лок чаще. Объём небольшой (несколько тысяч).
        rows = list(qs)
        self.stdout.write(f'Загружено в память: {len(rows)}')

        created = 0
        already = 0
        import time as _time
        from django.db import OperationalError

        def _retry(fn, *args, **kwargs):
            for attempt in range(8):
                try:
                    return fn(*args, **kwargs)
                except OperationalError as e:
                    if 'locked' not in str(e).lower() or attempt == 7:
                        raise
                    _time.sleep(0.5 * (attempt + 1))

        for h in rows:
            decl = (h.customs_declaration_number or '').strip()
            if not decl:
                continue
            existed = _retry(
                HawbDeclarationAttempt.objects.filter(
                    hawb=h, declaration_number=decl).exists)
            if existed:
                already += 1
                continue
            status = 'RELEASED' if h.release_date else 'FILED'
            _retry(_register_attempt, h, decl, status=status,
                   filed_date=h.filed_date,
                   release_date=h.release_date,
                   trigger_writeback=False)
            created += 1
            if created % 200 == 0:
                self.stdout.write(f'  {created} attempts created...')
        self.stdout.write(self.style.SUCCESS(
            f'\nСоздано attempt: {created}, уже было: {already}'))

        # Sheets writeback одним батчем для всех HAWB у которых есть attempt
        touched_ids = set(
            HawbDeclarationAttempt.objects.values_list('hawb_id', flat=True))
        if not touched_ids:
            return
        hawbs = list(HouseWaybill.objects.filter(pk__in=touched_ids))
        self.stdout.write(f'\nSheets writeback для {len(hawbs)} HAWB...')
        try:
            from cargo.services.sheets.writeback import (
                batch_write_attempts_count_for_hawbs,
            )
            n = batch_write_attempts_count_for_hawbs(hawbs)
            self.stdout.write(self.style.SUCCESS(
                f'  «Переподачи»: {n} cells'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'writeback failed: {e}'))
