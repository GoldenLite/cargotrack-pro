"""Переразобрать все ED.11003 → HawbCustomsRequest.

Удаляет существующие HawbCustomsRequest (создавались до фикса с
ProcessID-якорем) и заново вызывает dispatch на всех ED.11003 в БД.

Используется после изменений в apply_customs_request.

Запуск:
    python manage.py reapply_customs_requests
    python manage.py reapply_customs_requests --keep   # не удалять старые
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, HawbCustomsRequest
from cargo.services.alta.inbox import dispatch


class Command(BaseCommand):
    help = 'Удалить все HawbCustomsRequest и пересоздать через dispatch'

    def add_arguments(self, parser):
        parser.add_argument('--keep', action='store_true',
                            help='Не удалять существующие — только апсерт')

    def handle(self, *args, **opts):
        if not opts['keep']:
            n_del, _ = HawbCustomsRequest.objects.all().delete()
            self.stdout.write(f'Удалено: {n_del}')

        qs = AltaInboxMessage.objects.filter(msg_type='ED.11003')
        n = qs.count()
        self.stdout.write(f'ED.11003 для передиспатча: {n}')

        for m in qs.iterator():
            try:
                dispatch(m)
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f'  {m.envelope_id}: {e}'))

        total  = HawbCustomsRequest.objects.count()
        linked = HawbCustomsRequest.objects.exclude(hawb=None).count()
        self.stdout.write(self.style.SUCCESS(
            f'\nHawbCustomsRequest: {total}, привязано к HAWB: {linked}'))

        # Синхронно прогоняем writeback для всех HAWB у которых есть
        # запросы — нужно чтобы колонки «Запросы таможни» и «Количество
        # запросов» создались в Sheets даже если они ещё не существовали
        # (audit_sheets_vs_db не создаёт новые колонки сам).
        from cargo.models import HouseWaybill
        hawb_ids = set(HawbCustomsRequest.objects
                       .exclude(hawb=None)
                       .values_list('hawb_id', flat=True))
        if not hawb_ids:
            self.stdout.write('Sheets writeback: нет HAWB с запросами')
            return
        hawbs = list(HouseWaybill.objects.filter(pk__in=hawb_ids))
        self.stdout.write(f'\nSheets writeback для {len(hawbs)} HAWB...')
        try:
            from cargo.services.sheets.writeback import (
                batch_write_customs_requests_for_hawbs,
                batch_write_customs_requests_count_for_hawbs,
            )
            n1 = batch_write_customs_requests_for_hawbs(hawbs)
            n2 = batch_write_customs_requests_count_for_hawbs(hawbs)
            self.stdout.write(self.style.SUCCESS(
                f'  «Запросы таможни»:   {n1} cells'))
            self.stdout.write(self.style.SUCCESS(
                f'  «Количество запросов»: {n2} cells'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'writeback failed: {e}'))
