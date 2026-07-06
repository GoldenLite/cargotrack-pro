"""Стирает customs_declaration_number у HAWB которые в статусе REJECTED.
По бизнес-правилу: при отказе/отзыве рег.номер декларации не валиден
(декларация анулирована, переподача = новая ДТ).

Учитывается переподача: если у HAWB есть свежий inbox CMN.11337/11001 после
последнего rejected — НЕ стираем (это новый decl от новой подачи).

После стирания делает writeback в Sheets «Общее»/«Экспортная статистика».
"""
from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill, AltaInboxMessage


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        # HAWB в REJECTED с непустым decl.
        targets = list(HouseWaybill.objects.filter(
            customs_status='REJECTED'
        ).exclude(customs_declaration_number=''))
        self.stdout.write(f'REJECTED HAWB with decl: {len(targets)}')

        # Отфильтруем те, где есть свежая переподача после rejected.
        to_clean = []
        for h in targets:
            last_rejected = AltaInboxMessage.objects.filter(
                hawb=h, msg_kind='rejected'
            ).order_by('-prepared_at').values_list(
                'prepared_at', flat=True).first()
            if not last_rejected:
                # Странно: REJECTED но нет rejected msg. Пропускаем.
                continue
            newer_registered = AltaInboxMessage.objects.filter(
                hawb=h, msg_kind__in=('registered', 'released'),
                prepared_at__gt=last_rejected,
            ).exists()
            if newer_registered:
                continue  # новая подача → decl валиден
            to_clean.append(h)

        self.stdout.write(f'After resubmission filter: {len(to_clean)}')
        for h in to_clean[:20]:
            self.stdout.write(
                f'  {h.hawb_number} decl={h.customs_declaration_number!r}')
        if len(to_clean) > 20:
            self.stdout.write(f'  ... ещё {len(to_clean) - 20}')

        if opts['dry_run'] or not to_clean:
            return

        # Чистим decl + filed_date через UPDATE минуя save().
        HouseWaybill.objects.filter(
            pk__in=[h.pk for h in to_clean]
        ).update(customs_declaration_number='', filed_date=None)
        self.stdout.write(self.style.SUCCESS(
            f'cleaned {len(to_clean)} HAWB'))

        # Writeback в Sheets.
        for h in to_clean:
            h.refresh_from_db()
        from cargo.services.sheets.writeback import (
            batch_write_declarations_for_hawbs,
            batch_write_filed_dates_for_hawbs,
            batch_write_ed_status_for_hawbs,
        )
        batch_write_declarations_for_hawbs(to_clean)
        batch_write_filed_dates_for_hawbs(to_clean)
        batch_write_ed_status_for_hawbs(to_clean)
        self.stdout.write(self.style.SUCCESS('writeback done'))
