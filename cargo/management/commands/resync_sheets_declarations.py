"""Принудительный writeback ДТ в Sheets — batch-режим.

Google режет API на 60 req/min per user. write_declaration() в обычном
режиме делает 3-4 запроса на HAWB (open, read header, read cell, write),
на batch-resync 200+ HAWB упирается в rate-limit.

Эта команда группирует HAWB по worksheet, для каждого:
1. open_worksheet (1 запрос)
2. читает всю колонку «CargoTrack: ДТ» одним вызовом (1 запрос)
3. собирает diff в памяти
4. отправляет batch_update пачками по 100 ячеек (~ceil(N/100) запросов)

Итого на 266 HAWB в одном worksheet ~5 API-запросов вместо ~800.

Запуск:
    uv run python manage.py resync_sheets_declarations             # все с ДТ
    uv run python manage.py resync_sheets_declarations --cargo 190526-2
    uv run python manage.py resync_sheets_declarations --dry-run
"""
from __future__ import annotations

import time
from collections import defaultdict

import gspread
import gspread.exceptions
from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill, ImportedSheetRow, SheetSource
from cargo.services.sheets.client import SheetsConfigError, open_worksheet
from cargo.services.sheets.writeback import (
    CARGOTRACK_COL_HEADER, _ensure_cargotrack_column,
)


def _col_letter(col_idx: int) -> str:
    """1-based column index → A1 letter (1→A, 26→Z, 27→AA)."""
    result = ''
    n = col_idx
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(ord('A') + rem) + result
    return result


class Command(BaseCommand):
    help = 'Batch-резинк customs_declaration_number в Sheets'

    def add_arguments(self, parser):
        parser.add_argument('--cargo', default='', help='Только указанная Cargo')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--batch-size', type=int, default=100,
                            help='Размер пачки в batch_update (default 100)')
        parser.add_argument('--throttle-sec', type=float, default=2.0,
                            help='Пауза между batch_update (default 2s)')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        qs = HouseWaybill.objects.exclude(customs_declaration_number='').select_related('mawb')
        if opts['cargo']:
            qs = qs.filter(mawb__awb_number__iexact=opts['cargo'])
        if opts['limit']:
            qs = qs[:opts['limit']]

        pks = list(qs.values_list('pk', flat=True))
        self.stdout.write(f'HAWB с заполненной ДТ: {len(pks)}')

        # 1. Группировка HAWB по SheetSource через ImportedSheetRow
        sources: dict[int, SheetSource] = {}
        rows_by_source: dict[int, list] = defaultdict(list)
        # list of (row_index_1based, decl, hawb_number)
        no_row = 0
        for h in qs.iterator():
            row = (ImportedSheetRow.objects
                   .filter(source__kind='general',
                           hawb_number_norm__iexact=h.hawb_number)
                   .select_related('source')
                   .order_by('-last_imported_at')
                   .first())
            if not row:
                no_row += 1
                continue
            sources[row.source_id] = row.source
            rows_by_source[row.source_id].append(
                (row.source_row_index, h.customs_declaration_number.strip(), h.hawb_number)
            )

        self.stdout.write(f'  без строки в Sheets «Общее»: {no_row}')
        self.stdout.write(f'  Worksheet-ов задействовано: {len(sources)}')

        # 2. Для каждого worksheet — батч
        total_writes = 0
        total_skips = 0
        for source_id, items in rows_by_source.items():
            source = sources[source_id]
            self.stdout.write(f'\n=== {source.name} ({len(items)} HAWB) ===')

            try:
                ws = open_worksheet(source)
            except SheetsConfigError as e:
                self.stdout.write(f'  config error: {e}')
                continue
            except Exception as e:
                self.stdout.write(f'  open error: {e}')
                continue

            try:
                col = _ensure_cargotrack_column(ws, source.header_row)
            except gspread.exceptions.APIError as e:
                self.stdout.write(f'  ensure column failed: {e}')
                continue

            letter = _col_letter(col)

            # 3. Читаем всю колонку одним запросом
            try:
                col_values = ws.col_values(col)  # 1-based list of str
            except gspread.exceptions.APIError as e:
                self.stdout.write(f'  col_values failed: {e}')
                continue

            # 4. Собираем diff
            updates = []
            skipped = 0
            for row_idx, decl, hawb_number in items:
                existing = col_values[row_idx - 1] if row_idx - 1 < len(col_values) else ''
                if existing.strip() == decl:
                    skipped += 1
                    continue
                updates.append({
                    'range': f'{letter}{row_idx}',
                    'values': [[decl]],
                })

            self.stdout.write(f'  to write: {len(updates)}, already correct: {skipped}')
            total_skips += skipped

            if opts['dry_run'] or not updates:
                continue

            # 5. Batch update пачками
            batch_size = opts['batch_size']
            written_here = 0
            for i in range(0, len(updates), batch_size):
                batch = updates[i:i + batch_size]
                # Retry с backoff на 429
                for attempt in range(3):
                    try:
                        ws.batch_update(batch, value_input_option='USER_ENTERED')
                        written_here += len(batch)
                        break
                    except gspread.exceptions.APIError as e:
                        status = getattr(e.response, 'status_code', None)
                        if status == 429 and attempt < 2:
                            wait = 5 * (attempt + 1)
                            self.stdout.write(f'    429, sleep {wait}s')
                            time.sleep(wait)
                            continue
                        self.stdout.write(f'  batch fail: {e}')
                        break
                # Пауза между пачками — soft throttle
                if opts['throttle_sec']:
                    time.sleep(opts['throttle_sec'])
                self.stdout.write(f'    progress: {written_here}/{len(updates)} written in this source')

            total_writes += written_here

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. processed_hawbs={len(pks)} written={total_writes} '
            f'already_correct={total_skips} no_sheets_row={no_row}'
        ))
