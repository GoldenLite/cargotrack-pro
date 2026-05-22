"""Диагностика рассинхрона БД ↔ Sheets «CargoTrack: ДТ».

Для всех HAWB сравнивает значение в БД с тем что реально лежит в Sheets,
без записи. Группирует расхождения по типам:

- IN_SYNC               — БД и Sheets совпадают (хорошо)
- ONLY_IN_DB            — в БД ДТ есть, в Sheets пусто (надо resync)
- DIFFERENT             — в БД одна ДТ, в Sheets другая (конфликт; кто-то правит руками?)
- ONLY_IN_SHEETS        — в БД пусто, в Sheets есть (значит вручную добавили — не трогаем)
- NO_ROW_IN_SHEETS      — HAWB в БД с ДТ, но в Sheets «Общее» строки для неё нет
- DB_EMPTY              — оба пусты (нормально для невыпущенных)

Дальше можно прицельно лечить каждый класс.

Запуск:
    uv run python manage.py audit_sheets_sync             # все
    uv run python manage.py audit_sheets_sync --cargo 190526-2
    uv run python manage.py audit_sheets_sync --show ONLY_IN_DB  # вывести список
"""
from __future__ import annotations

from collections import defaultdict

import gspread.exceptions
from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill, ImportedSheetRow, SheetSource
from cargo.services.sheets.client import SheetsConfigError, open_worksheet
from cargo.services.sheets.writeback import _ensure_cargotrack_column


class Command(BaseCommand):
    help = 'Сравнить ДТ в БД vs в Google Sheets, без записи'

    def add_arguments(self, parser):
        parser.add_argument('--cargo', default='')
        parser.add_argument('--show', default='',
                            help='Категория для распечатки списка '
                                 '(ONLY_IN_DB / DIFFERENT / NO_ROW_IN_SHEETS / ONLY_IN_SHEETS)')
        parser.add_argument('--show-limit', type=int, default=50)

    def handle(self, *args, **opts):
        qs = HouseWaybill.objects.select_related('mawb').all()
        if opts['cargo']:
            qs = qs.filter(mawb__awb_number__iexact=opts['cargo'])

        hawbs = list(qs.only('pk', 'hawb_number', 'customs_declaration_number',
                              'mawb_id'))
        self.stdout.write(f'HAWB в выборке: {len(hawbs)}')

        # 1. Сматчить с ImportedSheetRow и сгруппировать по source
        rows_by_source: dict[int, dict] = defaultdict(dict)
        # source_id → {row_index: (hawb_number, db_decl)}
        sources: dict[int, SheetSource] = {}
        no_row_db_filled: list[tuple[str, str]] = []  # hawb_number, db_decl

        for h in hawbs:
            row = (ImportedSheetRow.objects
                   .filter(source__kind='general',
                           hawb_number_norm__iexact=h.hawb_number)
                   .select_related('source')
                   .order_by('-last_imported_at')
                   .first())
            db_decl = (h.customs_declaration_number or '').strip()
            if not row:
                if db_decl:
                    no_row_db_filled.append((h.hawb_number, db_decl))
                continue
            sources[row.source_id] = row.source
            rows_by_source[row.source_id][row.source_row_index] = (h.hawb_number, db_decl)

        # 2. Для каждого worksheet — один col_values на колонку CargoTrack: ДТ
        in_sync = 0
        only_db = []      # (hawb, db_decl)
        only_sheets = []  # (hawb, sheets_decl)
        different = []    # (hawb, db_decl, sheets_decl)
        db_empty = 0

        for source_id, by_row in rows_by_source.items():
            source = sources[source_id]
            try:
                ws = open_worksheet(source)
                col = _ensure_cargotrack_column(ws, source.header_row)
                col_values = ws.col_values(col)
            except (SheetsConfigError, gspread.exceptions.APIError) as e:
                self.stdout.write(f'  {source.name}: skip ({e})')
                continue
            except Exception as e:
                self.stdout.write(f'  {source.name}: error {e}')
                continue

            for row_idx, (hawb_num, db_decl) in by_row.items():
                sheets_val = (col_values[row_idx - 1].strip()
                              if row_idx - 1 < len(col_values) else '')
                if not db_decl and not sheets_val:
                    db_empty += 1
                elif db_decl and not sheets_val:
                    only_db.append((hawb_num, db_decl))
                elif sheets_val and not db_decl:
                    only_sheets.append((hawb_num, sheets_val))
                elif db_decl == sheets_val:
                    in_sync += 1
                else:
                    different.append((hawb_num, db_decl, sheets_val))

        # 3. Итог
        self.stdout.write(self.style.SUCCESS('\n=== Сводка ==='))
        self.stdout.write(f'  IN_SYNC          : {in_sync}')
        self.stdout.write(f'  ONLY_IN_DB       : {len(only_db)}   '
                          '(в БД ДТ есть, в Sheets пусто — нужен resync)')
        self.stdout.write(f'  DIFFERENT        : {len(different)} '
                          '(БД и Sheets расходятся)')
        self.stdout.write(f'  ONLY_IN_SHEETS   : {len(only_sheets)} '
                          '(в Sheets есть, в БД пусто — ручной ввод?)')
        self.stdout.write(f'  NO_ROW_IN_SHEETS : {len(no_row_db_filled)} '
                          '(HAWB с ДТ в БД, но ни в одной general-таблице её строки нет)')
        self.stdout.write(f'  DB_EMPTY         : {db_empty} (нормально, не выпущены)')

        show = (opts['show'] or '').upper()
        limit = opts['show_limit']
        if show == 'ONLY_IN_DB' and only_db:
            self.stdout.write(self.style.WARNING('\n=== ONLY_IN_DB ==='))
            for hn, d in only_db[:limit]:
                self.stdout.write(f'  {hn}  decl={d}')
        elif show == 'DIFFERENT' and different:
            self.stdout.write(self.style.WARNING('\n=== DIFFERENT ==='))
            for hn, db, s in different[:limit]:
                self.stdout.write(f'  {hn}  DB={db}  Sheets={s}')
        elif show == 'NO_ROW_IN_SHEETS' and no_row_db_filled:
            self.stdout.write(self.style.WARNING('\n=== NO_ROW_IN_SHEETS ==='))
            for hn, d in no_row_db_filled[:limit]:
                self.stdout.write(f'  {hn}  decl={d}')
        elif show == 'ONLY_IN_SHEETS' and only_sheets:
            self.stdout.write(self.style.WARNING('\n=== ONLY_IN_SHEETS ==='))
            for hn, s in only_sheets[:limit]:
                self.stdout.write(f'  {hn}  sheets={s}')
