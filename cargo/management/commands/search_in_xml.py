"""Найти упоминание произвольного номера (HAWB, MAWB, рег.№) в raw_xml.

Идёт по AltaInboxMessage.raw_xml. Полезно когда стандартные поля
(waybill_number_raw / common_waybill_number / declaration_number) пусты,
но искомый номер лежит где-то глубже в теле XML.

Запуск:
    python manage.py search_in_xml 10264758780 JK445350
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    help = 'Поиск произвольного номера в raw_xml AltaInboxMessage'

    def add_arguments(self, parser):
        parser.add_argument('term', nargs='+')
        parser.add_argument('--msg-type', default=None,
                            help='Фильтр по msg_type (например CMN.13029)')

    def handle(self, *args, **opts):
        for t in opts['term']:
            self.show(t, msg_type=opts['msg_type'])
            self.stdout.write('')

    def show(self, term: str, *, msg_type: str | None) -> None:
        self.stdout.write(self.style.NOTICE(
            f'\n{"="*60}\n  Поиск: {term}\n{"="*60}'))
        qs = AltaInboxMessage.objects.filter(raw_xml__icontains=term)
        if msg_type:
            qs = qs.filter(msg_type=msg_type)
        qs = qs.order_by('-prepared_at')[:30]
        msgs = list(qs)
        self.stdout.write(f'AltaInboxMessage с «{term}» в raw_xml: {len(msgs)}')
        for m in msgs:
            pm = m.parsed_meta or {}
            self.stdout.write(
                f'  {m.prepared_at} | {m.msg_type} kind={m.msg_kind} | '
                f'env={m.envelope_id} | cargo_id={m.cargo_id} '
                f'hawb_id={m.hawb_id} | wb={m.waybill_number_raw!r} | '
                f'lic={pm.get("svh_warehouse_license")!r} | '
                f'reg={pm.get("svh_do1_reg_number")!r}'
            )
