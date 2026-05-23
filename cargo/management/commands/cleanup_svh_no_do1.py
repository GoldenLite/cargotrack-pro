"""Стирает СВХ-данные у партий, для которых нет CMN.13010 в БД.

В первых итерациях apply_svh_placement писал в `Cargo.svh_do1_reg_number`
рег.номер представления (CMN.13029), который ОШИБОЧНО трактовался как
рег.номер ДО1. После уточнения логики apply_svh_placement пишет только
лицензию, а рег.номер ДО1 заполняет apply_svh_do1 (из CMN.13010). Но
у партий, для которых CMN.13010 ещё не пришёл, в Cargo остались легаси
значения. Эта команда их вычищает + чистит Sheets-ячейки.

Запуск:
    uv run python manage.py cleanup_svh_no_do1 --dry-run
    uv run python manage.py cleanup_svh_no_do1
"""
from __future__ import annotations

import time

import gspread.exceptions
from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, Cargo, ImportedSheetRow
from cargo.services.alta.inbox import OUR_WAREHOUSE_LICENSE
from cargo.services.sheets.client import SheetsConfigError, open_worksheet
from cargo.services.sheets.writeback import (
    CARGOTRACK_SVH_DATE_HEADER, CARGOTRACK_SVH_DO1_HEADER,
    CARGOTRACK_SVH_LICENSE_HEADER, _ensure_named_column,
)


def _col_letter(col_idx: int) -> str:
    result = ''
    n = col_idx
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(ord('A') + rem) + result
    return result


class Command(BaseCommand):
    help = 'Стирает svh_do1_reg_number и scan_into_bond у партий без CMN.13010 в БД'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        # Партии с нашей лицензией где есть данные, но нет привязанного ДО1
        candidates = Cargo.objects.filter(
            warehouse_license=OUR_WAREHOUSE_LICENSE,
        )

        orphans = []
        for c in candidates:
            has_do1 = AltaInboxMessage.objects.filter(
                cargo=c, msg_kind='svh_do1_registered',
            ).exists()
            if has_do1:
                continue
            # У партии нет ДО1 — все СВХ-данные легаси (из представления)
            if c.svh_do1_reg_number or c.scan_into_bond or c.warehouse_license:
                orphans.append(c)

        self.stdout.write(f'Партий с СВХ-данными но без CMN.13010: {len(orphans)}')
        for c in orphans:
            self.stdout.write(
                f'  {c.awb_number:<22} license={c.warehouse_license!r} '
                f'reg={c.svh_do1_reg_number!r} '
                f'date={c.scan_into_bond}'
            )

        if not orphans:
            return
        if opts['dry_run']:
            self.stdout.write(self.style.WARNING('DRY RUN — ничего не делаем'))
            return

        # 1. Стираем СВХ-поля в Cargo (включая лицензию)
        for c in orphans:
            c.svh_do1_reg_number = ''
            c.scan_into_bond = None
            c.warehouse_license = ''
            c.save(update_fields=['svh_do1_reg_number', 'scan_into_bond',
                                  'warehouse_license'])
        self.stdout.write(self.style.SUCCESS(f'Cargo: очищено {len(orphans)}'))

        # 2. Чистим ячейки в Sheets для всех HAWB этих партий
        hawb_nums = []
        for c in orphans:
            hawb_nums.extend(c.hawbs.values_list('hawb_number', flat=True))
        if not hawb_nums:
            self.stdout.write('  HAWB партий нет в БД — Sheets не трогаем')
            return

        rows = (ImportedSheetRow.objects
                .filter(source__kind='general', hawb_number_norm__in=hawb_nums)
                .select_related('source')
                .order_by('-last_imported_at'))
        if not rows.exists():
            self.stdout.write('  Sheets-строк нет')
            return

        # Группировка row_index по worksheet
        from collections import defaultdict
        rows_by_source = defaultdict(list)
        sources = {}
        seen = set()
        for r in rows:
            if r.hawb_number_norm in seen:
                continue
            seen.add(r.hawb_number_norm)
            sources[r.source_id] = r.source
            rows_by_source[r.source_id].append(r.source_row_index)

        for source_id, indices in rows_by_source.items():
            source = sources[source_id]
            self.stdout.write(f'  {source.name}: {len(indices)} HAWB')
            try:
                ws = open_worksheet(source)
                col_lic  = _ensure_named_column(ws, source.header_row,
                                                CARGOTRACK_SVH_LICENSE_HEADER)
                col_date = _ensure_named_column(ws, source.header_row,
                                                CARGOTRACK_SVH_DATE_HEADER)
                col_do1  = _ensure_named_column(ws, source.header_row,
                                                CARGOTRACK_SVH_DO1_HEADER)
            except (SheetsConfigError, gspread.exceptions.APIError) as e:
                self.stdout.write(f'    skip: {e}')
                continue

            # batch_update с пустыми значениями — это и есть очистка
            updates = []
            letter_lic  = _col_letter(col_lic)
            letter_date = _col_letter(col_date)
            letter_do1  = _col_letter(col_do1)
            for row_idx in indices:
                updates.append({'range': f'{letter_lic}{row_idx}',  'values': [['']]})
                updates.append({'range': f'{letter_date}{row_idx}', 'values': [['']]})
                updates.append({'range': f'{letter_do1}{row_idx}',  'values': [['']]})

            try:
                ws.batch_update(updates, value_input_option='USER_ENTERED')
                self.stdout.write(self.style.SUCCESS(
                    f'    очищено {len(updates)} ячеек'))
            except gspread.exceptions.APIError as e:
                self.stdout.write(f'    batch_update failed: {e}')
            time.sleep(1)
