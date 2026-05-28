"""Полная диагностика по HAWB и/или Cargo.

Печатает БД-поля, связанную партию, наличие строки в Sheets «Общее»,
все входящие/исходящие CMN-сообщения. Используется чтобы понять, почему
конкретные накладные не дотягиваются в Sheets.

Запуск:
    python manage.py inspect_hawb 10264758780 10262753015
    python manage.py inspect_hawb --cargo JK445350
    python manage.py inspect_hawb --cargo JK445350 10264758780
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone as tz

from cargo.models import (AltaInboxMessage, AltaOutboxObservation, Cargo,
                          HouseWaybill, ImportedSheetRow)


class Command(BaseCommand):
    help = 'Полная диагностика HAWB / Cargo (БД + Sheets + CMN сообщения)'

    def add_arguments(self, parser):
        parser.add_argument('hawb', nargs='*')
        parser.add_argument('--cargo', action='append', default=[],
                            help='MAWB / awb_number партии (можно повторить)')

    def handle(self, *args, **opts):
        for c in opts['cargo']:
            self.show_cargo(c)
            self.stdout.write('')
        for h in opts['hawb']:
            self.show_hawb(h)
            self.stdout.write('')

    def show_cargo(self, awb: str) -> None:
        self.stdout.write(self.style.NOTICE(
            f'\n{"="*60}\n  CARGO: {awb}\n{"="*60}'))
        c = Cargo.objects.filter(awb_number=awb).first()
        if not c:
            self.stdout.write('Cargo НЕ найден в БД')
            return
        self.stdout.write(f'pk={c.pk} stage={c.stage}')
        self.stdout.write(f'  warehouse_license: {c.warehouse_license!r}')
        self.stdout.write(f'  svh_do1_reg_number: {c.svh_do1_reg_number!r}')
        self.stdout.write(f'  scan_into_bond:     {c.scan_into_bond}')
        self.stdout.write(f'  release_date:       {c.release_date}')

        hawbs = list(HouseWaybill.objects.filter(mawb=c)
                     .only('hawb_number', 'filed_date', 'release_date',
                           'customs_declaration_number', 'svh_do1_gross_weight',
                           'svh_do1_place_count')
                     .order_by('hawb_number'))
        self.stdout.write(f'\nHAWB этой партии в БД: {len(hawbs)}')
        for h in hawbs:
            fd = tz.localtime(h.filed_date) if h.filed_date else None
            rd = tz.localtime(h.release_date) if h.release_date else None
            self.stdout.write(
                f'  {h.hawb_number} | decl={h.customs_declaration_number!r} | '
                f'filed={fd} | release={rd} | '
                f'wt={h.svh_do1_gross_weight} pl={h.svh_do1_place_count}'
            )

        outs = list(AltaOutboxObservation.objects.filter(
            common_waybill_number=awb).order_by('-prepared_at')[:10])
        self.stdout.write(f'\nAltaOutboxObservation для MAWB={awb}: {len(outs)}')
        for o in outs:
            self.stdout.write(
                f'  {o.prepared_at} | {o.msg_type} | env={o.envelope_id}')

    def show_hawb(self, hawb_number: str) -> None:
        self.stdout.write(self.style.NOTICE(
            f'\n{"="*60}\n  HAWB: {hawb_number}\n{"="*60}'))
        h = HouseWaybill.objects.filter(
            hawb_number__iexact=hawb_number).first()
        if not h:
            self.stdout.write('HAWB НЕ найден в БД')
        else:
            fd = tz.localtime(h.filed_date) if h.filed_date else None
            rd = tz.localtime(h.release_date) if h.release_date else None
            self.stdout.write(f'pk={h.pk} mawb_id={h.mawb_id}')
            self.stdout.write(f'  customs_declaration_number: '
                              f'{h.customs_declaration_number!r}')
            self.stdout.write(f'  filed_date  (MSK): {fd}')
            self.stdout.write(f'  release_date(MSK): {rd}')
            self.stdout.write(f'  svh_do1_gross_weight: {h.svh_do1_gross_weight}')
            self.stdout.write(f'  svh_do1_place_count:  {h.svh_do1_place_count}')
            self.stdout.write(f'  goods_count:          {h.goods_count}')
            if h.mawb_id:
                c = Cargo.objects.filter(pk=h.mawb_id).first()
                if c:
                    self.stdout.write(f'  mawb (Cargo): {c.awb_number}')

        # ImportedSheetRow
        rows = ImportedSheetRow.objects.filter(
            source__kind='general',
            hawb_number_norm__iexact=hawb_number,
        )
        self.stdout.write(f'\nImportedSheetRow «Общее»: {rows.count()}')
        for r in rows[:3]:
            d = r.data or {}
            self.stdout.write(
                f'  row {r.source_row_index} ТСД={d.get("ТСД")!r}')
            for k in ('Регистрационный номер ДТ', 'Дата выпуска ДТ',
                     'Дата подачи ДТ', 'CargoTrack: рег. номер ДТ',
                     'CargoTrack: дата подачи', 'CargoTrack: дата выпуска',
                     'Количество позиций'):
                v = d.get(k)
                if v:
                    self.stdout.write(f'    {k!r}: {v!r}')

        # CMN.11350 (release/withdrawn/rejected)
        inbox = AltaInboxMessage.objects.filter(
            waybill_number_raw__iexact=hawb_number).order_by('received_at')
        inbox_list = list(inbox)
        self.stdout.write(f'\nAltaInboxMessage: {len(inbox_list)}')
        for m in inbox_list:
            pm = m.parsed_meta or {}
            self.stdout.write(
                f'  {m.received_at} | {m.msg_type} kind={m.msg_kind} | '
                f'env={m.envelope_id} | '
                f'decl={pm.get("declaration_number")!r} | '
                f'reg_date={pm.get("registration_date")!r}'
            )

        # Outbox observations (CMN.11023/11349 contain this HAWB)
        outbox = (AltaOutboxObservation.objects
                  .filter(msg_type__in=['CMN.11023', 'CMN.11349'])
                  .filter(prepared_at__isnull=False))
        matched = []
        for o in outbox.iterator():
            hawbs = (o.parsed_meta or {}).get('hawbs') or []
            keys = [str(x).strip().upper() for x in hawbs]
            if hawb_number.upper() in keys:
                matched.append(o)
        self.stdout.write(f'\nCMN.11023/11349 содержат эту HAWB: {len(matched)}')
        for o in matched:
            local = tz.localtime(o.prepared_at) if o.prepared_at else None
            pm = o.parsed_meta or {}
            extras = []
            if 'goods_count' in pm:
                extras.append(f'goods_count={pm["goods_count"]}')
            per = pm.get('goods_count_per_hawb') or {}
            if per:
                extras.append(f'per_hawb[{hawb_number}]='
                              f'{per.get(hawb_number, "—")}')
            if 'raw_xml' in pm:
                extras.append(f'raw_xml={len(pm["raw_xml"])} bytes')
            self.stdout.write(
                f'  {o.msg_type} env={o.envelope_id} | MSK={local} | '
                + (' | '.join(extras) if extras else 'no goods_count meta'))
