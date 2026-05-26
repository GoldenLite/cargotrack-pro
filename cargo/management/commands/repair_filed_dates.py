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
from cargo.services.sheets.writeback import (
    _local_date_str, batch_write_filed_dates_for_hawbs,
)


class Command(BaseCommand):
    help = 'Заменить filed_date с time=00:00 на точное из CMN.11023/11349.prepared_at'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--force-resync', action='store_true',
                            help='Перезатереть Sheets для всех HAWB с точным '
                                 'filed_date в БД (исправляет стейл-ячейки, '
                                 'которые когда-то были записаны как 00:00).')

    def handle(self, *args, **opts):
        # Все HAWB с filed_date — потенциальные кандидаты.
        with_date = HouseWaybill.objects.filter(
            filed_date__isnull=False
        ).only('pk', 'hawb_number', 'filed_date')

        if opts['force_resync']:
            return self._force_resync(with_date, dry_run=opts['dry_run'])

        from django.utils import timezone as _tz
        def _is_midnight_msk(dt):
            local = _tz.localtime(dt) if _tz.is_aware(dt) else dt
            return not (local.hour or local.minute or local.second or local.microsecond)
        midnight_total = 0
        precise_total  = 0
        for h in with_date:
            if _is_midnight_msk(h.filed_date):
                midnight_total += 1
            else:
                precise_total += 1
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
            for h in touched:
                h.refresh_from_db(fields=['filed_date'])
            n = batch_write_filed_dates_for_hawbs(touched)
            self.stdout.write(self.style.SUCCESS(
                f'Sheets writeback: {n} cells'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(
                f'writeback failed: {e}'))

    def _force_resync(self, with_date, *, dry_run: bool) -> None:
        """Прогоняет writeback по всем HAWB с точным filed_date в БД.

        Полезно когда в БД time != 00:00, а в Sheets ячейка стейл (=00:00)
        потому что в момент первой записи в БД ещё было только date.
        """
        from django.utils import timezone as _tz
        def _precise(dt):
            local = _tz.localtime(dt) if _tz.is_aware(dt) else dt
            return bool(local.hour or local.minute or local.second or local.microsecond)
        precise = [h for h in with_date if _precise(h.filed_date)]
        self.stdout.write(self.style.NOTICE(
            f'Force-resync filed_date в Sheets для {len(precise)} HAWB '
            f'(с точным временем в БД)'))
        if dry_run:
            for h in precise[:10]:
                self.stdout.write(
                    f'  [DRY] {h.hawb_number}: {_local_date_str(h.filed_date)}')
            if len(precise) > 10:
                self.stdout.write(f'  ... ещё {len(precise)-10}')
            return
        try:
            n = batch_write_filed_dates_for_hawbs(precise)
            self.stdout.write(self.style.SUCCESS(
                f'Sheets writeback: {n} cells обновлено'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(
                f'writeback failed: {e}'))
