"""Сводка: где у нас HAWB без ДТ и есть ли вообще шанс что мы можем
восстановить ДТ из inbox-сообщений на VPS.

Показывает топ Cargo по числу пустых HAWB-ов с разрезом:
- has_release: в Cargo есть >=1 released/withdrawn-сообщение, привязанное
- has_unmatched: есть released-сообщения с hawb_number этих HAWB в raw_xml, но cargo=None
- no_data: вообще нет release-сообщений с упоминанием HAWB-ов этой партии

Запуск:
    uv run python manage.py audit_missing_declarations
    uv run python manage.py audit_missing_declarations --top 30
"""
from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand
from django.db.models import Count

from cargo.models import AltaInboxMessage, Cargo, HouseWaybill


class Command(BaseCommand):
    help = 'Аудит HAWB без ДТ + есть ли release-сообщения для их Cargo'

    def add_arguments(self, parser):
        parser.add_argument('--top', type=int, default=20)

    def handle(self, *args, **opts):
        # 1. Общие цифры
        all_hawbs = HouseWaybill.objects.count()
        empty = HouseWaybill.objects.filter(customs_declaration_number='').count()
        with_decl = all_hawbs - empty
        self.stdout.write(self.style.SUCCESS('=== Общие цифры ==='))
        self.stdout.write(f'  Всего HAWB: {all_hawbs}')
        self.stdout.write(f'  С ДТ:       {with_decl}')
        self.stdout.write(f'  Без ДТ:     {empty}')

        # 2. Топ Cargo по числу пустых HAWB-ов
        empty_by_cargo = (HouseWaybill.objects
                          .filter(customs_declaration_number='',
                                  mawb__isnull=False)
                          .values('mawb_id', 'mawb__awb_number')
                          .annotate(c=Count('id'))
                          .order_by('-c'))[:opts['top']]

        self.stdout.write(self.style.SUCCESS(f'\n=== Топ-{opts["top"]} Cargo по пустым HAWB ==='))

        # 3. Для каждой Cargo — есть ли release-сообщение
        for row in empty_by_cargo:
            cargo_id = row['mawb_id']
            cargo_num = row['mawb__awb_number']
            n_empty = row['c']

            # released с cargo=cargo_id
            n_release_attached = AltaInboxMessage.objects.filter(
                cargo_id=cargo_id, msg_kind__in=('released', 'withdrawn')
            ).count()

            # Сэмпл одной пустой HAWB-номера этой партии — проверим, есть ли
            # её упоминание в released-сообщениях БЕЗ cargo привязки
            sample_hawbs = list(HouseWaybill.objects.filter(
                mawb_id=cargo_id, customs_declaration_number=''
            ).values_list('hawb_number', flat=True)[:5])
            any_unmatched = False
            for hn in sample_hawbs:
                if AltaInboxMessage.objects.filter(
                    msg_kind__in=('released', 'withdrawn'),
                    cargo__isnull=True,
                    raw_xml__icontains=hn,
                ).exists():
                    any_unmatched = True
                    break

            tag = (
                'HAS_RELEASE_ATTACHED' if n_release_attached
                else 'UNMATCHED_RELEASE_EXISTS' if any_unmatched
                else 'NO_DATA'
            )
            self.stdout.write(
                f'  {cargo_num:<22} empty={n_empty:<4}  '
                f'attached_release={n_release_attached:<3}  {tag}'
            )

        # 4. Сводка по released-сообщениям с cargo=None
        unmatched_release = AltaInboxMessage.objects.filter(
            msg_kind__in=('released', 'withdrawn'),
            cargo__isnull=True,
        ).count()
        attached_release = AltaInboxMessage.objects.filter(
            msg_kind__in=('released', 'withdrawn'),
            cargo__isnull=False,
        ).count()
        self.stdout.write(self.style.SUCCESS('\n=== Released/withdrawn-сообщения ==='))
        self.stdout.write(f'  с cargo:     {attached_release}')
        self.stdout.write(f'  без cargo:   {unmatched_release}  '
                          f'(можно матчить через raw_xml если HAWB найдена)')
