"""Передиспатчить все AltaInboxMessage указанных типов.

Полезно после изменения MSG_KIND_MAP (например для добавления CMN.11337/11001
как 'registered') — старые сообщения сидят с прежним kind='info' и нужен
повторный dispatch чтобы classify+match+recompute сработали по новой логике.

После dispatch'а: для затронутых HAWB — writeback экспортных колонок (если
HAWB export) или просто оставить как есть (импортные подхватятся обычным
путём).

Запуск:
    uv run python manage.py reclassify_msg_type CMN.11337 CMN.11001
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, HouseWaybill
from cargo.services.alta.inbox import dispatch


class Command(BaseCommand):
    help = 'Передиспатч AltaInboxMessage по типам — переклассификация'

    def add_arguments(self, parser):
        parser.add_argument('msg_types', nargs='+')
        parser.add_argument('--limit', type=int, default=0)

    def handle(self, *args, **opts):
        from cargo.services.sheets.writeback import (
            begin_batch_writeback, end_batch_writeback,
        )

        qs = AltaInboxMessage.objects.filter(
            msg_type__in=opts['msg_types']).order_by('prepared_at')
        if opts['limit']:
            qs = qs[:opts['limit']]
        total = qs.count() if hasattr(qs, 'count') else len(list(qs))
        self.stdout.write(f'Сообщений для передиспатча: {total}')

        # Подавляем per-HAWB Sheets writeback — 746+93 CMN.11337/11001 ×
        # batch writeback каждый = quota storm. В конце один проход по
        # затронутым export-HAWB.
        begin_batch_writeback()
        touched_hawb_pks: set = set()
        ok = 0
        err = 0
        try:
            for m in qs:
                try:
                    dispatch(m)
                    ok += 1
                    if m.hawb_id:
                        touched_hawb_pks.add(m.hawb_id)
                except Exception as e:
                    err += 1
                    if err < 10:
                        self.stdout.write(f'  ERR #{m.pk}: {e}')
        finally:
            end_batch_writeback()

        self.stdout.write(self.style.SUCCESS(
            f'\nГотово. OK={ok}, ERR={err}, touched HAWB={len(touched_hawb_pks)}'))

        # Writeback всех экспортных колонок для затронутых HAWB.
        if touched_hawb_pks:
            export_hawbs = list(
                HouseWaybill.objects.filter(
                    pk__in=touched_hawb_pks, shipment_type='EXPORT')
            )
            self.stdout.write(
                f'EXPORT HAWB среди затронутых: {len(export_hawbs)}')
            if export_hawbs:
                from cargo.services.alta.outbox import _writeback_export_hawbs
                _writeback_export_hawbs(export_hawbs)
                self.stdout.write(self.style.SUCCESS('  export writeback готов'))
