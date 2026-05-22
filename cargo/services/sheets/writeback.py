"""Writeback из CargoTrack обратно в Google Sheets.

Цель: после того как у HAWB появился customs_declaration_number (через
inbox от таможни или ручной ввод), записать его в новую колонку
«CargoTrack: ДТ» в таблице «Общее» — рядом с X «Регистрационный номер ДТ»,
которую сотрудники продолжают вести руками. Ручную колонку НЕ трогаем.

Защита от лишних API-вызовов (Google биллит каждый write):
- читаем текущее значение ячейки перед записью
- пишем только если не совпадает
- кеш индекса нашей колонки на процесс (нет смысла дёргать шапку при каждой
  записи)
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import gspread
import gspread.exceptions

from cargo.models import HouseWaybill, ImportedSheetRow, SheetSource

from .client import SheetsConfigError, open_worksheet


logger = logging.getLogger('cargo.sheets.writeback')

# Имя нашей колонки в шапке таблицы «Общее»
CARGOTRACK_COL_HEADER = 'CargoTrack: ДТ'

# Кеш индекса колонки на процесс — {worksheet_id: 1-based col_index}
_col_index_cache: dict[str, int] = {}


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


def _ensure_cargotrack_column(ws: gspread.Worksheet, header_row: int) -> int:
    """Возвращает 1-based индекс нашей колонки, создавая её при необходимости.

    Логика:
    - читаем заголовочную строку
    - если CARGOTRACK_COL_HEADER уже есть — возвращаем индекс
    - иначе пишем заголовок в первую пустую колонку справа от существующих
    """
    cache_key = f'{ws.spreadsheet.id}:{ws.id}'
    cached = _col_index_cache.get(cache_key)
    if cached:
        return cached

    header_values = ws.row_values(header_row)
    for idx, value in enumerate(header_values, start=1):
        if (value or '').strip() == CARGOTRACK_COL_HEADER:
            _col_index_cache[cache_key] = idx
            return idx

    # Нет — добавляем в первую свободную справа
    new_col_idx = len(header_values) + 1
    ws.update_cell(header_row, new_col_idx, CARGOTRACK_COL_HEADER)
    _col_index_cache[cache_key] = new_col_idx
    logger.info('Created column "%s" at index %d in worksheet %s',
                CARGOTRACK_COL_HEADER, new_col_idx, ws.title)
    return new_col_idx


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
