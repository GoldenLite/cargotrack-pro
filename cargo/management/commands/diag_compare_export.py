"""Сравнить export-вкладку с DB для указанных HAWB."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill, ImportedSheetRow, SheetSource
from cargo.services.alta.ed_status import compute_ed_status
from cargo.services.sheets.client import open_worksheet


TRACKED = [
    'Номер накладной',
    'Номер транспортного документа',
    'Регистрационный номер ДТ',
    'Тип декларации',
    'Декларант',
    'Статус ЭД',
    'Дата подачи',
    'Дата выпуска',
    'Кол-во позиций',
]


def _fmt_dt(d):
    if not d:
        return ''
    try:
        import django.utils.timezone as tz
        local = d.astimezone(tz.get_current_timezone())
        return local.strftime('%d.%m.%Y %H:%M:%S')
    except Exception:
        return str(d)


class Command(BaseCommand):
    help = 'Сверка Sheets-export с DB для указанных HAWB.'

    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='+')

    def handle(self, *args, **opts):
        src = SheetSource.objects.filter(kind='export', is_active=True).first()
        if not src:
            self.stdout.write('Нет export-source')
            return
        ws = open_worksheet(src)
        header = ws.row_values(src.header_row)
        col_idx = {h: header.index(h) + 1 for h in TRACKED if h in header}
        col_data = {h: ws.col_values(ci) for h, ci in col_idx.items()}

        for hn in opts['hawbs']:
            self.stdout.write(f'\n=== {hn} ===')
            r = ImportedSheetRow.objects.filter(
                source=src, hawb_number_norm__iexact=hn).first()
            if not r:
                self.stdout.write('  нет в ImportedSheetRow(export)')
                continue
            row = r.source_row_index
            h = HouseWaybill.objects.filter(hawb_number__iexact=hn).first()
            if not h:
                self.stdout.write('  нет в HouseWaybill')
                continue
            db = {
                'Номер накладной':                h.hawb_number,
                'Номер транспортного документа':  (h.mawb.awb_number if h.mawb_id and h.mawb else ''),
                'Регистрационный номер ДТ':       h.customs_declaration_number or '',
                'Тип декларации':                 getattr(h, 'declaration_form', '') or '',
                'Декларант':                      getattr(h, 'declarant_name', '') or '',
                'Статус ЭД':                      compute_ed_status(h),
                'Дата подачи':                    _fmt_dt(h.filed_date),
                'Дата выпуска':                   _fmt_dt(h.release_date),
                'Кол-во позиций':                 (str(h.goods_count) if h.goods_count else ''),
            }
            self.stdout.write(f'  row={row}')
            for hdr, ci in col_idx.items():
                vals = col_data.get(hdr, [])
                sv = (vals[row - 1] if row - 1 < len(vals) else '').strip()
                dv = db.get(hdr, '<not-tracked>')
                marker = '  ' if sv == dv else '!!'
                self.stdout.write(f'  {marker} {hdr!r}')
                self.stdout.write(f'      Sheets: {sv!r}')
                self.stdout.write(f'      DB:     {dv!r}')
