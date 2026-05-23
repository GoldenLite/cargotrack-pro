"""Writeback из CargoTrack обратно в Google Sheets.

Цель: после того как у HAWB/Cargo появилась новая информация (ДТ из ED-ответа
таможни, дата размещения на СВХ из ДО1, лицензия СВХ) — записать её в наши
«CargoTrack: *»-колонки в таблице «Общее». Ручные колонки сотрудников НЕ
трогаем — добавляем свои справа.

Защита от лишних API-вызовов (Google биллит каждый write):
- читаем текущее значение ячейки перед записью
- пишем только если не совпадает
- кеш индекса колонки на процесс (нет смысла дёргать шапку при каждой записи)
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Optional

import gspread
import gspread.exceptions

from cargo.models import Cargo, HouseWaybill, ImportedSheetRow, SheetSource

from .client import SheetsConfigError, open_worksheet


logger = logging.getLogger('cargo.sheets.writeback')

# Имена наших колонок в шапке таблицы «Общее».
# Порядок здесь = порядок добавления справа от существующих.
CARGOTRACK_COL_HEADER         = 'CargoTrack: ДТ'
CARGOTRACK_SVH_LICENSE_HEADER = 'CargoTrack: лицензия СВХ'
CARGOTRACK_SVH_DATE_HEADER    = 'CargoTrack: дата ДО1'
CARGOTRACK_SVH_DO1_HEADER     = 'CargoTrack: рег. номер ДО1'

# Старые заголовки которые мы один раз использовали (содержание было неверным —
# данные представления вместо ДО1). Команда cleanup_svh_legacy_columns
# очищает их в Sheets, после чего сотрудник может удалить колонку руками.
LEGACY_SVH_HEADERS = (
    'CargoTrack: дата размещения',  # теперь = «дата ДО1»
)

# Кеш индекса колонки на процесс — {(worksheet_key, header): 1-based col_index}
_col_index_cache: dict[tuple[str, str], int] = {}


def _find_general_row(hawb: HouseWaybill) -> Optional[tuple[SheetSource, int]]:
    """Ищет в каком SheetSource (kind=general) лежит строка с этим HAWB.

    Возвращает (source, row_index_1based) или None.
    """
    if not hawb.hawb_number:
        return None
    row = (
        ImportedSheetRow.objects
        .filter(source__kind='general', hawb_number_norm__iexact=hawb.hawb_number)
        .select_related('source')
        .order_by('-last_imported_at')
        .first()
    )
    if not row:
        return None
    return (row.source, row.source_row_index)


def _ensure_named_column(ws: gspread.Worksheet, header_row: int,
                         header_name: str) -> int:
    """Возвращает 1-based индекс колонки `header_name`, создавая её при необходимости.

    Generic helper: используется для всех наших «CargoTrack: *»-колонок
    (ДТ, лицензия СВХ, дата размещения и будущих). Идемпотентно — если
    колонка уже есть в шапке, возвращает её индекс из кеша.

    Если колонки нет, ДОБАВЛЯЕТ её в первую свободную справа от всех
    существующих и записывает заголовок. Это значит порядок наших колонок
    в Sheets — это порядок их первого появления (т.е. ДТ → лицензия → дата,
    в порядке вызовов).
    """
    ws_key = f'{ws.spreadsheet.id}:{ws.id}'
    cache_key = (ws_key, header_name)
    cached = _col_index_cache.get(cache_key)
    if cached:
        return cached

    header_values = ws.row_values(header_row)
    for idx, value in enumerate(header_values, start=1):
        if (value or '').strip() == header_name:
            _col_index_cache[cache_key] = idx
            return idx

    # Нет — добавляем в первую свободную справа
    new_col_idx = len(header_values) + 1
    ws.update_cell(header_row, new_col_idx, header_name)
    _col_index_cache[cache_key] = new_col_idx
    logger.info('Created column "%s" at index %d in worksheet %s',
                header_name, new_col_idx, ws.title)
    return new_col_idx


def _ensure_cargotrack_column(ws: gspread.Worksheet, header_row: int) -> int:
    """Совместимость: тонкая обёртка над `_ensure_named_column` для колонки ДТ."""
    return _ensure_named_column(ws, header_row, CARGOTRACK_COL_HEADER)


def _col_letter(col_idx: int) -> str:
    """1-based column index → A1 letter (1→A, 26→Z, 27→AA)."""
    result = ''
    n = col_idx
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(ord('A') + rem) + result
    return result


def write_svh_placement_for_cargo(cargo: Cargo) -> int:
    """Пишет лицензию СВХ и дату размещения в Sheets для всех HAWB партии.

    СВХ-данные (CMN.13029) на уровне Cargo (партии), но строки в Sheets
    «Общее» индексированы по HAWB. Эта функция:
    1. Достаёт лицензию и дату из cargo (выставленных в apply_svh_placement).
    2. Находит все HAWB партии → их строки в Sheets.
    3. Группирует по SheetSource (обычно одна — таблица «Общее»).
    4. Делает batch_update по 2 ячейкам на HAWB одним запросом.

    Возвращает кол-во записанных ячеек (для логирования).
    """
    lic = (cargo.warehouse_license or '').strip()
    placed_dt = cargo.scan_into_bond
    do1_reg = (cargo.svh_do1_reg_number or '').strip()
    if not (lic or placed_dt or do1_reg):
        return 0  # нечего писать

    # Дата для Sheets — русский формат дд.мм.гггг (как у сотрудников
    # в остальных колонках таблицы «Общее»).
    placed_str = placed_dt.strftime('%d.%m.%Y') if placed_dt else ''

    # Все HAWB партии
    hawbs = list(cargo.hawbs.values_list('hawb_number', flat=True))
    if not hawbs:
        return 0

    # Группировка row_index по SheetSource — типично всё в одной general-таблице
    rows = (ImportedSheetRow.objects
            .filter(source__kind='general', hawb_number_norm__in=hawbs)
            .select_related('source')
            .order_by('-last_imported_at'))
    if not rows.exists():
        logger.info('svh writeback: no Sheets rows for Cargo %s (%d hawbs)',
                    cargo.awb_number, len(hawbs))
        return 0

    sources: dict[int, SheetSource] = {}
    rows_by_source: dict[int, list[int]] = defaultdict(list)
    seen_hawb: set[str] = set()  # отсекаем дубли (исторические импорты)
    for r in rows:
        if r.hawb_number_norm in seen_hawb:
            continue
        seen_hawb.add(r.hawb_number_norm)
        sources[r.source_id] = r.source
        rows_by_source[r.source_id].append(r.source_row_index)

    total_writes = 0
    for source_id, row_indices in rows_by_source.items():
        source = sources[source_id]
        try:
            ws = open_worksheet(source)
        except SheetsConfigError as e:
            logger.warning('svh writeback: open failed for %s: %s', source.name, e)
            continue
        except Exception:
            logger.exception('svh writeback: open error for %s', source.name)
            continue

        try:
            col_lic  = _ensure_named_column(ws, source.header_row,
                                            CARGOTRACK_SVH_LICENSE_HEADER)
            col_date = _ensure_named_column(ws, source.header_row,
                                            CARGOTRACK_SVH_DATE_HEADER)
            col_do1  = _ensure_named_column(ws, source.header_row,
                                            CARGOTRACK_SVH_DO1_HEADER)
        except gspread.exceptions.APIError as e:
            logger.exception('svh writeback: ensure column failed: %s', e)
            continue

        # Читаем существующие значения трёх колонок одним запросом каждая,
        # чтобы не писать совпадающее (Google биллит каждый write).
        try:
            existing_lic  = ws.col_values(col_lic)
            existing_date = ws.col_values(col_date)
            existing_do1  = ws.col_values(col_do1)
        except gspread.exceptions.APIError as e:
            logger.exception('svh writeback: col_values failed: %s', e)
            continue

        letter_lic  = _col_letter(col_lic)
        letter_date = _col_letter(col_date)
        letter_do1  = _col_letter(col_do1)

        updates = []
        for row_idx in row_indices:
            cur_lic = (existing_lic[row_idx - 1]
                       if row_idx - 1 < len(existing_lic) else '').strip()
            cur_date = (existing_date[row_idx - 1]
                        if row_idx - 1 < len(existing_date) else '').strip()
            cur_do1 = (existing_do1[row_idx - 1]
                       if row_idx - 1 < len(existing_do1) else '').strip()
            if lic and cur_lic != lic:
                updates.append({'range': f'{letter_lic}{row_idx}', 'values': [[lic]]})
            if placed_str and cur_date != placed_str:
                updates.append({'range': f'{letter_date}{row_idx}', 'values': [[placed_str]]})
            if do1_reg and cur_do1 != do1_reg:
                updates.append({'range': f'{letter_do1}{row_idx}', 'values': [[do1_reg]]})

        if not updates:
            continue

        # Один batch_update на партию (типично ≤ 80 HAWB × 2 = 160 ячеек).
        # При больших партиях разбить можно, но Google допускает до 10к ячеек.
        backoff_steps = [2, 4, 8]
        for attempt in range(len(backoff_steps) + 1):
            try:
                ws.batch_update(updates, value_input_option='USER_ENTERED')
                total_writes += len(updates)
                logger.info('svh writeback Cargo %s: %d cells in %s',
                            cargo.awb_number, len(updates), source.name)
                break
            except gspread.exceptions.APIError as e:
                status = getattr(e.response, 'status_code', None)
                if status == 429 and attempt < len(backoff_steps):
                    wait = backoff_steps[attempt]
                    logger.warning('svh writeback 429, retry in %ds (attempt %d)',
                                   wait, attempt + 1)
                    time.sleep(wait)
                    continue
                logger.exception('svh writeback batch failed: %s', e)
                break

    return total_writes


def write_declaration(hawb: HouseWaybill) -> bool:
    """Записывает hawb.customs_declaration_number в Sheets-колонку «CargoTrack: ДТ».

    Возвращает True если что-то реально записали; False если no-op / нечего писать /
    ошибка. Никогда не падает — Exception ловятся и логируются.
    """
    decl = (hawb.customs_declaration_number or '').strip()
    if not decl:
        return False  # нечего писать

    found = _find_general_row(hawb)
    if not found:
        logger.info('writeback skipped: HAWB %s has no row in general sheet',
                    hawb.hawb_number)
        return False
    source, row_index = found

    # 429 — Google rate limit (60 writes/min на пользователя). Ретраим с
    # exponential backoff: 2s → 4s → 8s, потом сдаёмся. В batch-сценариях
    # без ретрая теряем целые пачки записей.
    backoff_steps = [2, 4, 8]
    for attempt in range(len(backoff_steps) + 1):
        try:
            ws = open_worksheet(source)
            col = _ensure_cargotrack_column(ws, source.header_row)

            current = ws.cell(row_index, col).value or ''
            if current.strip() == decl:
                return False  # уже там — идемпотент

            ws.update_cell(row_index, col, decl)
            logger.info('Wrote ДТ=%s into %s row=%d col=%d (HAWB %s)',
                        decl, source.name, row_index, col, hawb.hawb_number)
            return True
        except SheetsConfigError as e:
            logger.warning('writeback skipped (no credentials): %s', e)
            return False
        except gspread.exceptions.APIError as e:
            status = getattr(e.response, 'status_code', None)
            if status == 403:
                logger.error(
                    'writeback failed 403: service account нуждается в роли Editor '
                    'на Sheet «%s». См. share-диалог в Google Sheets.', source.name
                )
                return False
            elif status == 429 and attempt < len(backoff_steps):
                wait = backoff_steps[attempt]
                logger.warning('writeback rate-limit 429, retry in %ds (attempt %d)',
                               wait, attempt + 1)
                time.sleep(wait)
                continue
            else:
                logger.exception('writeback APIError for HAWB %s', hawb.hawb_number)
                return False
        except Exception:
            logger.exception('writeback unexpected error for HAWB %s', hawb.hawb_number)
            return False
    return False
