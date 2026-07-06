"""«Общее»: авто-заполнение столбца «Дата прибытия» датой регистрации ДО1.

Правило: где у партии есть ДО1 (Cargo.scan_into_bond) И ячейка «Дата прибытия»
ПУСТА → ставим дату ДО1 в формате дд.мм.гггг. Существующие значения НЕ трогаем
(fill-empty). Sort-proof: таргет по живой колонке «Накладная СДЭК».

Запускается cron'ом (авто-заполнение при появлении ДО1). Ручной запуск:
    manage.py set_arrival_from_do1            # dry-run
    manage.py set_arrival_from_do1 --apply
"""
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone
from gspread.utils import rowcol_to_a1

from cargo.models import HouseWaybill, SheetSource
from cargo.services.sheets.client import open_worksheet
from cargo.services.sheets.writeback import _chunked_batch_update

logger = logging.getLogger('cargo.arrival')

ARRIVE_HEADER = 'Дата прибытия'
HAWB_HEADERS = ('Накладная СДЭК', 'Номер накладной', 'HAWB')


class Command(BaseCommand):
    help = 'Ставит «Дата прибытия» = дата ДО1 (дд.мм.гггг) в ПУСТЫЕ ячейки «Общее».'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Реально записать (без флага — dry-run)')

    def handle(self, *args, **opts):
        apply = opts['apply']
        gen = SheetSource.objects.filter(kind='general', is_active=True).first()
        if not gen:
            self.stderr.write('general source не настроен')
            return

        ws = open_worksheet(gen)
        vals = ws.get_all_values()
        headers = vals[gen.header_row - 1]
        hawb_col = next((ci for ci, h in enumerate(headers)
                         if h.strip() in HAWB_HEADERS), None)
        arr_col = next((ci for ci, h in enumerate(headers)
                        if h.strip() == ARRIVE_HEADER), None)
        if hawb_col is None or arr_col is None:
            self.stderr.write(f'колонки не найдены (hawb={hawb_col} arrive={arr_col})')
            return

        # HAWB -> дата ДО1 (дд.мм.гггг), только у кого ДО1 есть
        hawb_date = {}
        for hn, sib in (HouseWaybill.objects
                        .filter(mawb__scan_into_bond__isnull=False)
                        .values_list('hawb_number', 'mawb__scan_into_bond')):
            hawb_date[hn] = timezone.localtime(sib).strftime('%d.%m.%Y')

        updates = []
        for ri in range(gen.header_row, len(vals)):
            row = vals[ri]
            if hawb_col >= len(row):
                continue
            d = hawb_date.get(row[hawb_col].strip())
            if not d:
                continue
            cur = row[arr_col].strip() if arr_col < len(row) else ''
            if cur:
                continue  # только ПУСТЫЕ — не перезаписываем
            updates.append({'range': rowcol_to_a1(ri + 1, arr_col + 1),
                            'values': [[d]]})

        self.stdout.write(f'к заполнению (пустых «Дата прибытия» с ДО1): {len(updates)}')
        if not apply:
            if updates:
                self.stdout.write('(dry-run — добавь --apply)')
            return
        if not updates:
            return

        # USER_ENTERED (внутри _chunked_batch_update): Sheets парсит
        # «13.06.2026» как настоящую ДАТУ, а не текст с префиксом ' —
        # иначе колонку нельзя сортировать хронологически. Хелпер также
        # копирует range на каждый retry (gspread мутирует dict in-place).
        written = _chunked_batch_update(ws, updates, 'arrival from do1', gen.name)
        logger.info('set_arrival_from_do1: filled %d cells', written)
        self.stdout.write(self.style.SUCCESS(f'ЗАПИСАНО {written}'))
