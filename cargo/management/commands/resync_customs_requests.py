"""Пересобрать ячейку «Запросы таможни» в Sheets для HAWB у которых
есть пачка ≥2 запросов в одном envelope. Используется после фикса
порядка сортировки в _customs_requests_text.

Также сбрасывает CrmHawbIndex.last_request у этих HAWB, чтобы
crm_sync_incremental заметил расхождение и переписал ячейку в CRM-вкладках.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand

from cargo.models import (CrmHawbIndex, HawbCustomsRequest, HouseWaybill)
from cargo.services.sheets.writeback import (
    batch_write_customs_requests_for_hawbs)


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true',
                            help='Пересинхр для всех HAWB с >=1 запросом')

    def handle(self, *args, **opts):
        if opts['all']:
            hawb_ids = set(HawbCustomsRequest.objects
                          .exclude(hawb__isnull=True)
                          .values_list('hawb_id', flat=True).distinct())
        else:
            # Только пачки ≥2 запросов в одном envelope.
            counts = defaultdict(lambda: defaultdict(int))
            for r in (HawbCustomsRequest.objects
                      .exclude(hawb__isnull=True)
                      .values('hawb_id', 'envelope_id')):
                counts[r['hawb_id']][r['envelope_id']] += 1
            hawb_ids = {hid for hid, envs in counts.items()
                        if any(n >= 2 for n in envs.values())}
        self.stdout.write(f'Target HAWB: {len(hawb_ids)}')
        if not hawb_ids:
            return

        hawbs = list(HouseWaybill.objects.filter(id__in=hawb_ids))

        # 1) «Общее» (writeback в основные Sheets).
        n = batch_write_customs_requests_for_hawbs(hawbs)
        self.stdout.write(f'Sheets «Общее»: обновлено {n} строк')

        # 2) Инвалидируем cache в CrmHawbIndex, чтобы инкрементальный sync
        #    переписал CRM-ячейки на следующем тике.
        affected = CrmHawbIndex.objects.filter(
            hawb_number__in=[h.hawb_number for h in hawbs]
        ).update(last_request='__resync_pending__')
        self.stdout.write(
            f'CrmHawbIndex.last_request → пометили {affected} строк')
