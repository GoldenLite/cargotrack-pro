"""Диагностика связки Cargo ↔ AltaOutboxObservation ↔ ImportedSheetRow.

Запуск:
    uv run python manage.py diag_alta_cargo 222-40333086
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Показывает что в БД и Sheets-row_indices для одного MAWB'

    def add_arguments(self, parser):
        parser.add_argument('mawb', help='MAWB партии (например 222-40333086)')

    def handle(self, *args, **opts):
        from cargo.models import (
            Cargo, HouseWaybill, AltaOutboxObservation, ImportedSheetRow,
        )

        mawb = opts['mawb']
        c = Cargo.objects.filter(awb_number=mawb).first()
        self.stdout.write('')
        if not c:
            self.stdout.write(self.style.WARNING(f'Cargo {mawb}: НЕ найден в БД'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Cargo {mawb}:'))
            self.stdout.write(f'  warehouse_license  = {c.warehouse_license!r}')
            self.stdout.write(f'  warehouse (FK)     = {c.warehouse}')
            self.stdout.write(f'  scan_into_bond     = {c.scan_into_bond}')
            self.stdout.write(f'  svh_do1_reg_number = {c.svh_do1_reg_number!r}')
            self.stdout.write(f'  stage              = {c.stage}')
            self.stdout.write(f'  HAWBs в БД         = {c.hawbs.count()}')

        self.stdout.write('')
        self.stdout.write(self.style.NOTICE(
            f'-- AltaOutboxObservation common_waybill_number={mawb!r} --'))
        obs_qs = (AltaOutboxObservation.objects
                  .filter(common_waybill_number=mawb)
                  .order_by('-prepared_at'))
        if not obs_qs.exists():
            self.stdout.write('  (пусто)')
        for o in obs_qs[:10]:
            pm = o.parsed_meta or {}
            cert = pm.get('certificate_number') or ''
            hawbs = pm.get('hawbs') or []
            goods = pm.get('goods') or {}
            raw = pm.get('raw_xml') or ''
            self.stdout.write(
                f'  #{o.pk} {o.msg_type} prepared_at={o.prepared_at}\n'
                f'    cert       = {cert!r}\n'
                f'    hawbs ({len(hawbs)}) = {hawbs[:3]}{"..." if len(hawbs) > 3 else ""}\n'
                f'    goods cnt  = {len(goods)}\n'
                f'    raw_xml    = {len(raw)} chars'
            )

        # Если есть HAWB в БД — посмотрим что в Sheets-индексах
        if c and c.hawbs.exists():
            self.stdout.write('')
            self.stdout.write(self.style.NOTICE(
                '-- HAWB → ImportedSheetRow.source_row_index --'))
            self.stdout.write(f'  {"HAWB":<15} {"row_idx":>8}  {"svh_do1_sent_at"}')
            for h in c.hawbs.order_by('hawb_number')[:20]:
                r = (ImportedSheetRow.objects
                     .filter(source__kind='general',
                             hawb_number_norm=h.hawb_number)
                     .order_by('-last_imported_at').first())
                ridx = r.source_row_index if r else '—'
                self.stdout.write(
                    f'  {h.hawb_number:<15} {str(ridx):>8}  {h.svh_do1_sent_at}')

        # Возможно raw_xml в обсервации содержит ДРУГОЙ MAWB — выкусим и сравним
        for o in obs_qs.filter(msg_type='ED.DO1')[:3]:
            raw = (o.parsed_meta or {}).get('raw_xml') or ''
            if not raw:
                continue
            from cargo.services.alta.xml_extract import parse_do1_report
            parsed = parse_do1_report(raw)
            self.stdout.write('')
            self.stdout.write(self.style.NOTICE(
                f'-- re-parse raw_xml obs #{o.pk} --'))
            self.stdout.write(f'  parsed mawb         = {parsed.get("mawb")!r}')
            self.stdout.write(f'  parsed certificate  = {parsed.get("certificate_number")!r}')
            self.stdout.write(f'  parsed hawbs ({len(parsed.get("hawbs") or [])}) = '
                              f'{(parsed.get("hawbs") or [])[:3]}')
