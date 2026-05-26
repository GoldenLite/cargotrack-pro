"""Диагностика: что у HAWB в filed_date (БД, МСК) и что есть в CMN.11023/11349.

Пример: накладная 10246473928 реально подавалась 25.05 11:28:48 МСК, но в
Sheets видим 25.05 00:00:00. Команда покажет:
- filed_date в БД (UTC + MSK).
- есть ли AltaOutboxObservation CMN.11023/11349 которая содержит этот HAWB
  в parsed_meta['hawbs'], и какой у неё prepared_at (UTC + MSK).
- AltaInboxMessage CMN.11350 для этой HAWB (там filed_date = registration_date
  без времени → это источник 00:00).

Запуск:
    python manage.py inspect_hawb_filed_date 10246473928 10264127739
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone as tz

from cargo.models import (AltaInboxMessage, AltaOutboxObservation,
                          HouseWaybill)


class Command(BaseCommand):
    help = 'Показать filed_date и связанные CMN для HAWB'

    def add_arguments(self, parser):
        parser.add_argument('hawb', nargs='+')

    def handle(self, *args, **opts):
        for hn in opts['hawb']:
            self.show(hn)
            self.stdout.write('')

    def show(self, hawb_number: str) -> None:
        self.stdout.write(self.style.NOTICE(
            f'\n{"="*60}\n  HAWB: {hawb_number}\n{"="*60}'))

        h = HouseWaybill.objects.filter(
            hawb_number__iexact=hawb_number).first()
        if not h:
            self.stdout.write('HAWB не найден в БД')
            return

        if h.filed_date:
            local = tz.localtime(h.filed_date)
            self.stdout.write(f'filed_date (UTC): {h.filed_date}')
            self.stdout.write(f'filed_date (MSK): {local} '
                              f'(time={local.strftime("%H:%M:%S")})')
        else:
            self.stdout.write('filed_date: None')
        self.stdout.write(f'customs_declaration_number: '
                          f'{h.customs_declaration_number!r}')

        # CMN.11023/11349 outbox observations с этой HAWB в parsed_meta['hawbs']
        obs = (AltaOutboxObservation.objects
               .filter(msg_type__in=['CMN.11023', 'CMN.11349'])
               .filter(prepared_at__isnull=False)
               .order_by('prepared_at'))
        matched = []
        for o in obs.iterator():
            hawbs = (o.parsed_meta or {}).get('hawbs') or []
            keys = [str(x).strip().upper() for x in hawbs]
            if hawb_number.upper() in keys:
                matched.append(o)

        self.stdout.write(
            f'\nCMN.11023/11349 (наши исходящие) содержат эту HAWB: {len(matched)}')
        for o in matched:
            local = tz.localtime(o.prepared_at) if o.prepared_at else None
            self.stdout.write(
                f'  {o.msg_type} env={o.envelope_id} | '
                f'prepared_at UTC={o.prepared_at} | MSK={local}')

        # CMN.11350 (входящий релиз) для этой HAWB — источник 00:00 filed_date
        inbox = (AltaInboxMessage.objects
                 .filter(msg_type='CMN.11350')
                 .filter(waybill_number_raw__iexact=hawb_number)
                 .order_by('received_at'))
        inbox_list = list(inbox)
        self.stdout.write(
            f'\nCMN.11350 (входящие, релизы): {len(inbox_list)}')
        for m in inbox_list:
            pm = m.parsed_meta or {}
            self.stdout.write(
                f'  env={m.envelope_id} | kind={m.msg_kind} | '
                f'reg_date={pm.get("registration_date")!r} | '
                f'decl={pm.get("declaration_number")!r}')
