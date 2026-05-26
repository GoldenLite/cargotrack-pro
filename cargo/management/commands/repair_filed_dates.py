"""Исправить filed_date с time=00:00:00 на точное время из CMN.11023/11349.

Источник точного времени: AltaOutboxObservation(msg_type IN ('CMN.11023',
'CMN.11349')).prepared_at. В parsed_meta['hawbs'] лежит весь список HAWB
этой подачи. Для каждой HAWB в списке у которой filed_date с time=00:00
(значит ставился ранее из CMN.11350.registration_date) — записать
prepared_at.

Запуск:
    python manage.py repair_filed_dates --dry-run
    python manage.py repair_filed_dates
"""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db.models import Q

from cargo.models import AltaOutboxObservation, HouseWaybill
from cargo.services.alta.outbox import _filed_date_should_replace


class Command(BaseCommand):
    help = 'Заменить filed_date с time=00:00 на точное из CMN.11023/11349.prepared_at'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        # Все HAWB с filed_date — потенциальные кандидаты.
        with_date = HouseWaybill.objects.filter(
            filed_date__isnull=False
        ).only('pk', 'hawb_number', 'filed_date')

        midnight_total = 0
        precise_total  = 0
        for h in with_date:
            if (h.filed_date.hour or h.filed_date.minute
                    or h.filed_date.second or h.filed_date.microsecond):
                precise_total += 1
            else:
                midnight_total += 1
        self.stdout.write(f'HouseWaybill с filed_date: {with_date.count()}')
        self.stdout.write(f'  с точным временем (>0): {precise_total}')
        self.stdout.write(f'  с 00:00:00 (требуют ремонта): {midnight_total}')

        # Индекс HAWB → лучший prepared_at из observations
        # (CMN.11023/11349). Берём минимальный prepared_at.
        observations = (
            AltaOutboxObservation.objects
            .filter(msg_type__in=['CMN.11023', 'CMN.11349'])
            .filter(prepared_at__isnull=False)
            .only('prepared_at', 'parsed_meta')
        )
        best: dict[str, object] = {}
        for obs in observations.iterator():
            hawbs = (obs.parsed_meta or {}).get('hawbs') or []
            for hn in hawbs:
                key = str(hn).strip().upper()
                cur = best.get(key)
                if cur is None or obs.prepared_at < cur:
                    best[key] = obs.prepared_at
        self.stdout.write(f'CMN.11023/11349 в БД: {observations.count()} '
                          f'(уникальных HAWB: {len(best)})')

        # Обходим только HAWB с filed_date — кандидатов на правку.
        updates: list[tuple[HouseWaybill, object]] = []
        for h in with_date:
            key = (h.hawb_number or '').strip().upper()
            target = best.get(key)
            if not target:
                continue
            if _filed_date_should_replace(h.filed_date, target):
                updates.append((h, target))

        self.stdout.write(self.style.NOTICE(
            f'\nПодлежат ремонту: {len(updates)} HAWB'))
        for h, t in updates[:15]:
            self.stdout.write(
                f'  {h.hawb_number}: {h.filed_date} → {t}')
        if len(updates) > 15:
            self.stdout.write(f'  ... ещё {len(updates)-15}')

        if opts['dry_run'] or not updates:
            return

        # Применяем + writeback
        touched: list[HouseWaybill] = []
        for h, t in updates:
            HouseWaybill.objects.filter(pk=h.pk).update(filed_date=t)
            touched.append(h)

        self.stdout.write(self.style.SUCCESS(
            f'Обновлено в БД: {len(touched)}'))

        try:
            from cargo.services.sheets.writeback import (
                batch_write_filed_dates_for_hawbs,
            )
            for h in touched:
                h.refresh_from_db(fields=['filed_date'])
            n = batch_write_filed_dates_for_hawbs(touched)
            self.stdout.write(self.style.SUCCESS(
                f'Sheets writeback: {n} cells'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(
                f'writeback failed: {e}'))
