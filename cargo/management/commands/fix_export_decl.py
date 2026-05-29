"""Принудительно вызвать recompute_declaration для каждой EXPORT-HAWB.

После reclassify_msg_type и нескольких dispatch'ей могут остаться HAWB
у которых customs_declaration_number пуст, хотя в parsed_meta привязанных
inbox-сообщений есть полный рег.номер. Эта команда обходит проблему,
вызывая recompute_declaration напрямую.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill
from cargo.services.alta.inbox import recompute_declaration


class Command(BaseCommand):
    help = 'recompute_declaration для всех EXPORT-HAWB'

    def handle(self, *args, **opts):
        from cargo.services.sheets.writeback import (
            begin_batch_writeback, end_batch_writeback,
        )

        hawbs = list(HouseWaybill.objects.filter(shipment_type='EXPORT')
                     .select_related('mawb'))
        self.stdout.write(f'EXPORT HAWB: {len(hawbs)}')

        begin_batch_writeback()
        updated = 0
        try:
            for h in hawbs:
                before = h.customs_declaration_number
                try:
                    recompute_declaration(h.mawb, h)
                except Exception as e:
                    self.stdout.write(f'  ERR {h.hawb_number}: {e}')
                    continue
                h.refresh_from_db(fields=['customs_declaration_number'])
                if h.customs_declaration_number and not before:
                    updated += 1
                    if updated <= 20:
                        self.stdout.write(
                            f'  {h.hawb_number}: {h.customs_declaration_number}')
        finally:
            end_batch_writeback()

        self.stdout.write(self.style.SUCCESS(
            f'\nЗаписано рег.номеров: {updated}'))

        # Все в writeback одним проходом
        if updated:
            self.stdout.write('\nWriteback export-колонок...')
            try:
                from cargo.services.alta.outbox import _writeback_export_hawbs
                touched = [h for h in hawbs if h.customs_declaration_number]
                _writeback_export_hawbs(touched)
                self.stdout.write(self.style.SUCCESS('  готово'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  failed: {e}'))
