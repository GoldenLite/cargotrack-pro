"""Сравнить наши CargoTrack-колонки в «Общее» с тем что в БД.

Использовать для глазной проверки ПЕРЕД массовым writeback'ом.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill, ImportedSheetRow, SheetSource
from cargo.services.sheets.client import open_worksheet

TRACKED_HEADERS = [
    'CargoTrack: ДТ',
    'CargoTrack: Дата подачи',
    'CargoTrack: Дата выпуска',
    'CargoTrack: статус ЭД',
    'CargoTrack: Кол-во позиций',
    'CargoTrack: Запросы таможни',
    'CargoTrack: Количество запросов',
    'CargoTrack: Переподачи',
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
    help = 'Сравнить Sheets-значения «Общее» с DB для указанных HAWB.'

    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='+')

    def handle(self, *args, **opts):
        src = SheetSource.objects.filter(
            kind='general', is_active=True).first()
        if not src:
            self.stdout.write('Нет general-source')
            return
        ws = open_worksheet(src)
        header = ws.row_values(src.header_row)

        col_idx = {h: header.index(h) + 1 for h in TRACKED_HEADERS
                   if h in header}
        if not col_idx:
            self.stdout.write('Не нашёл CargoTrack-колонок в шапке')
            return

        # Читаем все нужные колонки одним проходом.
        col_data = {}
        for h, ci in col_idx.items():
            col_data[h] = ws.col_values(ci)

        for hn in opts['hawbs']:
            self.stdout.write(f'\n=== {hn} ===')
            r = ImportedSheetRow.objects.filter(
                source=src, hawb_number_norm__iexact=hn).first()
            if not r:
                self.stdout.write('  нет в ImportedSheetRow')
                continue
            row = r.source_row_index
            self.stdout.write(f'  row={row}')

            h_db = HouseWaybill.objects.filter(
                hawb_number__iexact=hn).first()
            if not h_db:
                self.stdout.write('  нет в HouseWaybill')
                continue

            from cargo.services.alta.ed_status import compute_ed_status

            db_vals = {
                'CargoTrack: ДТ':                 h_db.customs_declaration_number or '',
                'CargoTrack: Дата подачи':        _fmt_dt(h_db.filed_date),
                'CargoTrack: Дата выпуска':       _fmt_dt(h_db.release_date),
                'CargoTrack: статус ЭД':          compute_ed_status(h_db),
                'CargoTrack: Кол-во позиций':     (str(h_db.goods_count)
                                                    if h_db.goods_count else ''),
                'CargoTrack: Переподачи':         (str(
                    h_db.declaration_attempts.count() - 1)
                    if h_db.declaration_attempts.count() > 1 else ''),
            }
            for h, ci in col_idx.items():
                values = col_data.get(h, [])
                sheet_val = (values[row - 1] if row - 1 < len(values)
                             else '').strip()
                db_val = db_vals.get(h, '<not-tracked>')
                marker = '  ' if sheet_val == db_val else '!!'
                self.stdout.write(
                    f'  {marker} {h!r}')
                self.stdout.write(f'      Sheets: {sheet_val!r}')
                self.stdout.write(f'      DB:     {db_val!r}')
