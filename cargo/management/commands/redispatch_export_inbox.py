"""Передиспатчить все AltaInboxMessage привязанные к экспортным HAWB.

После изменения логики recompute_declaration (рег.номер пишется при любом
сообщении с GTDNumber, не только released/withdrawn) старые сообщения
сидят в БД с прежним результатом. Эта команда проходит по ним и заново
вызывает dispatch — пересчитывает customs_declaration_number и
триггерит writeback в Sheets «Экспортная статистика».

Запуск:
    uv run python manage.py redispatch_export_inbox
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, HouseWaybill
from cargo.services.alta.inbox import dispatch


class Command(BaseCommand):
    help = 'Передиспатч inbox-сообщений экспортных HAWB + ed_status писеback'

    def handle(self, *args, **opts):
        export_hawb_pks = list(
            HouseWaybill.objects.filter(shipment_type='EXPORT')
            .values_list('pk', flat=True)
        )
        self.stdout.write(f'EXPORT HAWB: {len(export_hawb_pks)}')

        msgs = list(AltaInboxMessage.objects.filter(
            hawb_id__in=export_hawb_pks
        ).order_by('prepared_at'))
        self.stdout.write(f'inbox-сообщений для редиспатча: {len(msgs)}')

        ok = 0
        err = 0
        for m in msgs:
            try:
                dispatch(m)
                ok += 1
            except Exception as e:
                err += 1
                self.stdout.write(f'  ERR #{m.pk}: {e}')

        self.stdout.write(self.style.SUCCESS(
            f'\nДиспатч завершён. OK={ok}, ERR={err}'))

        # ed_status и весь export-writeback для всех экспортных HAWB.
        hawbs = list(HouseWaybill.objects.filter(pk__in=export_hawb_pks))
        if hawbs:
            self.stdout.write(f'\nWriteback всех export-колонок ({len(hawbs)} HAWB)...')
            try:
                from cargo.services.sheets.writeback import (
                    ensure_export_rows_for_hawbs,
                    batch_write_transport_doc_for_hawbs,
                    batch_write_declarations_for_hawbs,
                    batch_write_filed_dates_for_hawbs,
                    batch_write_release_dates_for_hawbs,
                    batch_write_goods_count_for_hawbs,
                    batch_write_customs_requests_for_hawbs,
                    batch_write_customs_requests_count_for_hawbs,
                    batch_write_attempts_count_for_hawbs,
                    batch_write_declaration_form_for_hawbs,
                    batch_write_ed_status_for_hawbs,
                )
                ensure_export_rows_for_hawbs(hawbs)
                batch_write_transport_doc_for_hawbs(hawbs)
                batch_write_declarations_for_hawbs(hawbs)
                batch_write_filed_dates_for_hawbs(hawbs)
                batch_write_release_dates_for_hawbs(hawbs)
                batch_write_goods_count_for_hawbs(hawbs)
                batch_write_customs_requests_for_hawbs(hawbs)
                batch_write_customs_requests_count_for_hawbs(hawbs)
                batch_write_attempts_count_for_hawbs(hawbs)
                batch_write_declaration_form_for_hawbs(hawbs)
                batch_write_ed_status_for_hawbs(hawbs)
                self.stdout.write(self.style.SUCCESS('  готово'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  writeback failed: {e}'))
