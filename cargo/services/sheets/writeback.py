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
import threading
import time
from collections import defaultdict
from typing import Optional

import gspread
import gspread.exceptions

from cargo.models import Cargo, HouseWaybill, ImportedSheetRow, SheetSource

from .client import SheetsConfigError, open_worksheet


logger = logging.getLogger('cargo.sheets.writeback')

# ── Подавление сигналов writeback во время batch-операций ────────────
# Когда apply_status в inbox.py обрабатывает multi-waybill релиз (49 HAWB
# одной декларации), 49 × change_customs_status() = 49 post_save сигналов,
# каждый стартует фоновый поток с per-HAWB writeback'ом (2-3 reads на Sheets
# API). 100+ reads моментально → Google quota 300/min превышается.
# Решение: на время batch-обработки подавляем сигналы, в конце делаем
# ОДИН batch-writeback на все HAWB сразу.
_signal_suppressor = threading.local()


def begin_batch_writeback() -> None:
    """Подавляет per-HAWB сигналы writeback в текущем потоке."""
    _signal_suppressor.active = True


def end_batch_writeback() -> None:
    """Снимает подавление."""
    _signal_suppressor.active = False


def signals_suppressed() -> bool:
    """True если текущий поток находится в batch-режиме."""
    return getattr(_signal_suppressor, 'active', False)

# Имена наших колонок в шапке таблицы «Общее».
# Порядок здесь = порядок добавления справа от существующих.
CARGOTRACK_COL_HEADER          = 'CargoTrack: ДТ'
CARGOTRACK_SVH_LICENSE_HEADER  = 'CargoTrack: лицензия СВХ'
CARGOTRACK_SVH_DATE_HEADER     = 'CargoTrack: дата ДО1'
CARGOTRACK_SVH_DO1_HEADER      = 'CargoTrack: рег. номер ДО1'
CARGOTRACK_FILED_DATE_HEADER   = 'CargoTrack: дата подачи'
CARGOTRACK_RELEASE_DATE_HEADER = 'CargoTrack: дата выпуска'

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


def _local_date_str(dt) -> str:
    """Форматирует aware-datetime в дд.мм.гггг чч:мм:сс по settings.TIME_ZONE.

    Юзер просит фиксировать конкретное время события. Для событий с источником
    только-дата (filed_date из CMN.RegistrationDate, scan_into_bond без времени)
    время показывается как 00:00:00 — это означает «событие в этот день,
    точное время в CMN не приходит». Для событий с настоящим timestamp
    (release_date из CMN.DecisionDate, scan_into_bond с DO1RegTime) —
    реальное время в МСК.

    strftime не конвертирует TZ — берёт дату как есть из datetime. Если в БД
    лежит UTC «2026-05-22 21:00:00+00:00» (= 23.05 МСК по факту), прямой
    strftime даст «22.05.2026». timezone.localtime() переводит в MSK перед
    форматированием. Для naive datetime — strftime напрямую.
    """
    if dt is None:
        return ''
    from django.utils import timezone as _tz
    if _tz.is_aware(dt):
        dt = _tz.localtime(dt)
    return dt.strftime('%d.%m.%Y %H:%M:%S')


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
    # Не делаем early-return при пустых значениях — функция должна
    # уметь ОЧИЩАТЬ Sheets-ячейки если данные были откачены на стороне БД.

    # Дата для Sheets — русский формат дд.мм.гггг по МСК (как у сотрудников
    # в остальных колонках таблицы «Общее»). _local_date_str переводит из
    # UTC если datetime aware — иначе strftime напрямую сдвигал бы дату.
    placed_str = _local_date_str(placed_dt)

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
            # Пишем даже если значение пустое — нужно чтобы при откате
            # неверной CMN.13010-привязки (Cargo.scan_into_bond=None и
            # т.п.) Sheets-ячейки тоже очищались, а не висели стейлом.
            if cur_lic != lic:
                updates.append({'range': f'{letter_lic}{row_idx}', 'values': [[lic]]})
            if cur_date != placed_str:
                updates.append({'range': f'{letter_date}{row_idx}', 'values': [[placed_str]]})
            if cur_do1 != do1_reg:
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


def batch_write_svh_for_cargos(cargos: list) -> int:
    """Batch-writeback СВХ-полей для списка партий — ОДИН проход по Sheets.

    Per-cargo `write_svh_placement_for_cargo` делает по 3 read'а на колонку,
    что упирается в Google quota 300 read/min уже на 100 партиях. Эта функция
    читает каждую из трёх колонок ОДИН раз для всей таблицы и собирает
    единый batch_update.

    Возвращает кол-во записанных ячеек.
    """
    if not cargos:
        return 0

    # Собираем все HAWB в одну выборку с привязкой к Cargo
    hawb_to_cargo: dict[str, Cargo] = {}
    for c in cargos:
        for hn in c.hawbs.values_list('hawb_number', flat=True):
            hawb_to_cargo[hn] = c
    if not hawb_to_cargo:
        return 0

    rows = (ImportedSheetRow.objects
            .filter(source__kind='general',
                    hawb_number_norm__in=list(hawb_to_cargo.keys()))
            .select_related('source')
            .order_by('-last_imported_at'))
    if not rows.exists():
        logger.info('batch svh: no Sheets rows for %d cargos', len(cargos))
        return 0

    # Группируем по source, дедупим по HAWB
    sources: dict[int, SheetSource] = {}
    rows_by_source: dict[int, list[tuple[int, Cargo]]] = defaultdict(list)
    seen: set[str] = set()
    for r in rows:
        if r.hawb_number_norm in seen:
            continue
        seen.add(r.hawb_number_norm)
        cargo = hawb_to_cargo.get(r.hawb_number_norm)
        if not cargo:
            continue
        sources[r.source_id] = r.source
        rows_by_source[r.source_id].append((r.source_row_index, cargo))

    total = 0
    for source_id, items in rows_by_source.items():
        source = sources[source_id]
        try:
            ws = open_worksheet(source)
            col_lic  = _ensure_named_column(ws, source.header_row,
                                            CARGOTRACK_SVH_LICENSE_HEADER)
            col_date = _ensure_named_column(ws, source.header_row,
                                            CARGOTRACK_SVH_DATE_HEADER)
            col_do1  = _ensure_named_column(ws, source.header_row,
                                            CARGOTRACK_SVH_DO1_HEADER)
        except (SheetsConfigError, gspread.exceptions.APIError) as e:
            logger.exception('batch svh: open/ensure failed: %s', e)
            continue

        # Читаем три колонки ОДИН РАЗ для всей таблицы
        try:
            existing_lic  = ws.col_values(col_lic)
            existing_date = ws.col_values(col_date)
            existing_do1  = ws.col_values(col_do1)
        except gspread.exceptions.APIError as e:
            logger.exception('batch svh: col_values failed: %s', e)
            continue

        letter_lic  = _col_letter(col_lic)
        letter_date = _col_letter(col_date)
        letter_do1  = _col_letter(col_do1)

        updates = []
        for row_idx, cargo in items:
            lic = (cargo.warehouse_license or '').strip()
            placed_str = _local_date_str(cargo.scan_into_bond)
            do1_reg = (cargo.svh_do1_reg_number or '').strip()

            cur_lic = (existing_lic[row_idx - 1]
                       if row_idx - 1 < len(existing_lic) else '').strip()
            cur_date = (existing_date[row_idx - 1]
                        if row_idx - 1 < len(existing_date) else '').strip()
            cur_do1 = (existing_do1[row_idx - 1]
                       if row_idx - 1 < len(existing_do1) else '').strip()

            # Пишем даже если значение пустое — нужно чтобы при откате
            # неверной CMN.13010-привязки (Cargo.scan_into_bond=None и
            # т.п.) Sheets-ячейки тоже очищались, а не висели стейлом.
            if cur_lic != lic:
                updates.append({'range': f'{letter_lic}{row_idx}', 'values': [[lic]]})
            if cur_date != placed_str:
                updates.append({'range': f'{letter_date}{row_idx}', 'values': [[placed_str]]})
            if cur_do1 != do1_reg:
                updates.append({'range': f'{letter_do1}{row_idx}', 'values': [[do1_reg]]})

        if not updates:
            continue

        # Один batch на всю выборку (Google допускает ~10k ячеек за раз)
        backoff_steps = [2, 4, 8, 16]
        for attempt in range(len(backoff_steps) + 1):
            try:
                ws.batch_update(updates, value_input_option='USER_ENTERED')
                total += len(updates)
                logger.info('batch svh: wrote %d cells in %s',
                            len(updates), source.name)
                break
            except gspread.exceptions.APIError as e:
                status = getattr(e.response, 'status_code', None)
                if status == 429 and attempt < len(backoff_steps):
                    wait = backoff_steps[attempt]
                    logger.warning('batch svh 429, retry in %ds', wait)
                    time.sleep(wait)
                    continue
                logger.exception('batch svh: batch_update failed: %s', e)
                break

    return total


def _write_hawb_date(hawb: HouseWaybill, value, header_name: str,
                     log_label: str) -> bool:
    """Generic per-HAWB date writeback в указанную CargoTrack-колонку.

    Используется для filed_date и release_date — логика одинаковая, отличается
    только источник значения (поле HAWB) и имя колонки. value — datetime или None.
    Формат дд.мм.гггг, идемпотент, ловит все исключения.
    """
    if not value:
        return False

    date_str = _local_date_str(value)

    found = _find_general_row(hawb)
    if not found:
        logger.info('%s writeback skipped: HAWB %s has no row in general',
                    log_label, hawb.hawb_number)
        return False
    source, row_index = found

    backoff_steps = [2, 4, 8]
    for attempt in range(len(backoff_steps) + 1):
        try:
            ws = open_worksheet(source)
            col = _ensure_named_column(ws, source.header_row, header_name)

            current = (ws.cell(row_index, col).value or '').strip()
            if current == date_str:
                return False

            ws.update_cell(row_index, col, date_str)
            logger.info('Wrote %s=%s into %s row=%d col=%d (HAWB %s)',
                        log_label, date_str, source.name, row_index, col,
                        hawb.hawb_number)
            return True
        except SheetsConfigError as e:
            logger.warning('%s writeback skipped (no creds): %s', log_label, e)
            return False
        except gspread.exceptions.APIError as e:
            status = getattr(e.response, 'status_code', None)
            if status == 429 and attempt < len(backoff_steps):
                wait = backoff_steps[attempt]
                logger.warning('%s writeback 429, retry in %ds', log_label, wait)
                time.sleep(wait)
                continue
            logger.exception('%s writeback APIError for HAWB %s',
                             log_label, hawb.hawb_number)
            return False
        except Exception:
            logger.exception('%s writeback error for HAWB %s',
                             log_label, hawb.hawb_number)
            return False
    return False


def write_filed_date_for_hawb(hawb: HouseWaybill) -> bool:
    """Пишет HouseWaybill.filed_date в Sheets-колонку «CargoTrack: дата подачи»."""
    return _write_hawb_date(hawb, hawb.filed_date,
                            CARGOTRACK_FILED_DATE_HEADER, 'filed_date')


def write_release_date_for_hawb(hawb: HouseWaybill) -> bool:
    """Пишет HouseWaybill.release_date в Sheets-колонку «CargoTrack: дата выпуска»."""
    return _write_hawb_date(hawb, hawb.release_date,
                            CARGOTRACK_RELEASE_DATE_HEADER, 'release_date')


def _batch_write_hawb_dates(hawbs: list, value_attr: str,
                            header_name: str, log_label: str) -> int:
    """Generic batch writeback per-HAWB даты — 1 col_values + 1 batch_update.

    Используется и для filed_date, и для release_date. value_attr — имя
    атрибута HAWB-объекта (например 'filed_date'), header_name — колонка в
    Sheets, log_label — префикс для логов.
    """
    if not hawbs:
        return 0

    # Включаем и HAWB с пустым value_attr — нужно чтобы при reparse
    # ячейка с ошибочно проставленной датой обнулилась (например,
    # release_date был выставлен по ошибке, потом таможня дала отказ
    # → новый статус REJECTED → release_date очищен → надо переписать
    # Sheets-ячейку пустой строкой).
    by_hawb: dict[str, HouseWaybill] = {
        h.hawb_number: h for h in hawbs if h.hawb_number
    }
    if not by_hawb:
        return 0

    rows = (ImportedSheetRow.objects
            .filter(source__kind='general',
                    hawb_number_norm__in=list(by_hawb.keys()))
            .select_related('source')
            .order_by('-last_imported_at'))
    if not rows.exists():
        logger.info('batch %s: no Sheets rows for %d hawbs', log_label, len(by_hawb))
        return 0

    sources: dict[int, SheetSource] = {}
    items_by_source: dict[int, list[tuple[int, HouseWaybill]]] = defaultdict(list)
    seen: set[str] = set()
    for r in rows:
        if r.hawb_number_norm in seen:
            continue
        seen.add(r.hawb_number_norm)
        h = by_hawb.get(r.hawb_number_norm)
        if not h:
            continue
        sources[r.source_id] = r.source
        items_by_source[r.source_id].append((r.source_row_index, h))

    total = 0
    for source_id, items in items_by_source.items():
        source = sources[source_id]
        try:
            ws = open_worksheet(source)
            col = _ensure_named_column(ws, source.header_row, header_name)
        except (SheetsConfigError, gspread.exceptions.APIError) as e:
            logger.exception('batch %s: open/ensure failed: %s', log_label, e)
            continue

        try:
            existing = ws.col_values(col)
        except gspread.exceptions.APIError as e:
            logger.exception('batch %s: col_values failed: %s', log_label, e)
            continue

        letter = _col_letter(col)
        updates = []
        for row_idx, h in items:
            value = getattr(h, value_attr)
            date_str = _local_date_str(value)
            cur = (existing[row_idx - 1]
                   if row_idx - 1 < len(existing) else '').strip()
            if cur != date_str:
                updates.append({'range': f'{letter}{row_idx}',
                                'values': [[date_str]]})

        if not updates:
            continue

        backoff_steps = [2, 4, 8, 16]
        for attempt in range(len(backoff_steps) + 1):
            try:
                ws.batch_update(updates, value_input_option='USER_ENTERED')
                total += len(updates)
                logger.info('batch %s: wrote %d cells in %s',
                            log_label, len(updates), source.name)
                break
            except gspread.exceptions.APIError as e:
                status = getattr(e.response, 'status_code', None)
                if status == 429 and attempt < len(backoff_steps):
                    wait = backoff_steps[attempt]
                    logger.warning('batch %s 429, retry in %ds', log_label, wait)
                    time.sleep(wait)
                    continue
                logger.exception('batch %s: batch_update failed', log_label)
                break

    return total


def batch_write_filed_dates_for_hawbs(hawbs: list) -> int:
    """Batch writeback filed_date — для resync_filed_dates."""
    return _batch_write_hawb_dates(hawbs, 'filed_date',
                                   CARGOTRACK_FILED_DATE_HEADER, 'filed_date')


def batch_write_release_dates_for_hawbs(hawbs: list) -> int:
    """Batch writeback release_date — для resync_release_dates."""
    return _batch_write_hawb_dates(hawbs, 'release_date',
                                   CARGOTRACK_RELEASE_DATE_HEADER, 'release_date')


def batch_write_declarations_for_hawbs(hawbs: list) -> int:
    """Batch writeback customs_declaration_number — 1 col_values + 1 batch_update.

    Аналог batch_write_*_dates но для строкового decl-номера. Используется в
    inbox.apply_status вместо 49 per-HAWB write_declaration вызовов при
    multi-waybill релизе.
    """
    if not hawbs:
        return 0

    # Включаем HAWB с пустым decl — нужно чтобы при переходе со статуса
    # RELEASED на HOLD/REJECTED ячейка ДТ в Sheets обнулилась (а не висела
    # со стейл-значением). Логика записи ниже сравнивает с текущим значением
    # ячейки и пишет '' только если в Sheets было что-то.
    by_hawb: dict[str, HouseWaybill] = {
        h.hawb_number: h for h in hawbs if h.hawb_number
    }
    if not by_hawb:
        return 0

    rows = (ImportedSheetRow.objects
            .filter(source__kind='general',
                    hawb_number_norm__in=list(by_hawb.keys()))
            .select_related('source')
            .order_by('-last_imported_at'))
    if not rows.exists():
        return 0

    sources: dict[int, SheetSource] = {}
    items_by_source: dict[int, list[tuple[int, HouseWaybill]]] = defaultdict(list)
    seen: set[str] = set()
    for r in rows:
        if r.hawb_number_norm in seen:
            continue
        seen.add(r.hawb_number_norm)
        h = by_hawb.get(r.hawb_number_norm)
        if not h:
            continue
        sources[r.source_id] = r.source
        items_by_source[r.source_id].append((r.source_row_index, h))

    total = 0
    for source_id, items in items_by_source.items():
        source = sources[source_id]
        try:
            ws = open_worksheet(source)
            col = _ensure_cargotrack_column(ws, source.header_row)
        except (SheetsConfigError, gspread.exceptions.APIError) as e:
            logger.exception('batch decl: open/ensure failed: %s', e)
            continue

        try:
            existing = ws.col_values(col)
        except gspread.exceptions.APIError as e:
            logger.exception('batch decl: col_values failed: %s', e)
            continue

        letter = _col_letter(col)
        updates = []
        for row_idx, h in items:
            decl = (h.customs_declaration_number or '').strip()
            cur = (existing[row_idx - 1]
                   if row_idx - 1 < len(existing) else '').strip()
            if cur != decl:
                updates.append({'range': f'{letter}{row_idx}',
                                'values': [[decl]]})

        if not updates:
            continue

        backoff_steps = [2, 4, 8, 16]
        for attempt in range(len(backoff_steps) + 1):
            try:
                ws.batch_update(updates, value_input_option='USER_ENTERED')
                total += len(updates)
                logger.info('batch decl: wrote %d cells in %s',
                            len(updates), source.name)
                break
            except gspread.exceptions.APIError as e:
                status = getattr(e.response, 'status_code', None)
                if status == 429 and attempt < len(backoff_steps):
                    wait = backoff_steps[attempt]
                    logger.warning('batch decl 429, retry in %ds', wait)
                    time.sleep(wait)
                    continue
                logger.exception('batch decl: batch_update failed')
                break

    return total


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
