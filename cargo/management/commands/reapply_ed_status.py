"""Пересчитать и записать ed_status + цвет для импортных HAWB.

Берёт все HAWB у которых может быть значимый ed_status:
- есть AltaInboxMessage значимый, ИЛИ
- есть release_date, ИЛИ
- есть HawbDeclarationAttempt, ИЛИ
- есть AltaOutboxObservation CMN.11023/11349 (наша подача).
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from cargo.models import HouseWaybill
from cargo.services.sheets.writeback import batch_write_ed_status_for_hawbs


class Command(BaseCommand):
    help = 'reapply ed_status (значение + цвет) для импортных HAWB'

    def handle(self, *args, **opts):
        qs = HouseWaybill.objects.filter(
            shipment_type='IMPORT',
        ).filter(
            Q(release_date__isnull=False)
            | Q(declaration_attempts__isnull=False)
            | Q(inbox_messages__isnull=False)
        ).distinct()
        hawbs = list(qs.order_by('pk'))
        self.stdout.write(f'IMPORT HAWB кандидатов: {len(hawbs)}')
        n = batch_write_ed_status_for_hawbs(hawbs)
        self.stdout.write(self.style.SUCCESS(f'wrote {n} cells'))
