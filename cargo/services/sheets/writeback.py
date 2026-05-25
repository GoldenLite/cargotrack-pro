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
import re
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
# MAWB-привязка для HAWB — из ED.DO1 outbox observation (в Sheets «Общее»
# самой колонки MAWB у юзера нет, эту мы заполняем сами).
CARGOTRACK_CARGO_MAWB_HEADER   = 'CargoTrack: номер партии'
CARGOTRACK_SVH_LICENSE_HEADER  = 'CargoTrack: лицензия СВХ'
# Хронологический порядок: МЫ подали → таможня зарегистрировала.
CARGOTRACK_SVH_DO1_SENT_HEADER = 'CargoTrack: дата подачи ДО1'
CARGOTRACK_SVH_DATE_HEADER     = 'CargoTrack: дата регистрации ДО1'
CARGOTRACK_SVH_DO1_HEADER      = 'CargoTrack: рег. номер ДО1'
# Per-HAWB вес и места из <Goods> блоков ДО-1.
CARGOTRACK_SVH_DO1_WEIGHT_HEADER = 'CargoTrack: вес ДО1'
CARGOTRACK_SVH_DO1_PLACES_HEADER = 'CargoTrack: мест ДО1'
CARGOTRACK_SVH_DO2_DATE_HEADER = 'CargoTrack: дата ДО2'
# Юзер не использует рег.номер ДО2 в Sheets, колонку не создаём
# и не пишем (в БД хранится только дата ДО2 на HAWB).
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
                         header_name: str,
                         after_header: str = '') -> int:
    """Возвращает 1-based индекс колонки `header_name`, создавая её при необходимости.

    Generic helper: используется для всех наших «CargoTrack: *»-колонок
    (ДТ, лицензия СВХ, дата размещения и будущих). Идемпотентно — если
    колонка уже есть в шапке, возвращает её индекс из кеша.

    after_header: если задан, новую колонку вставляем СРАЗУ ПОСЛЕ колонки
    с этим заголовком (через insert_cols со сдвигом существующих). Если
    after_header не найден — fallback в первую свободную справа.

    Без after_header — добавляем в первую свободную справа от всех
    существующих и записываем заголовок (порядок наших колонок — порядок
    первого появления).
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

    # Колонки нет. Решаем, КУДА её вставить.
    if after_header:
        after_idx = 0
        for idx, value in enumerate(header_values, start=1):
            if (value or '').strip() == after_header:
                after_idx = idx
                break
        if after_idx > 0:
            new_col_idx = after_idx + 1
            # gspread.insert_cols сдвигает все колонки от new_col_idx вправо,
            # затем записываем шапку. value_input_option важен для USER_ENTERED.
            ws.insert_cols([['' for _ in range(ws.row_count)]], col=new_col_idx)
            ws.update_cell(header_row, new_col_idx, header_name)
            # Инвалидируем кеш для всех колонок этого ws — индексы сдвинулись.
            for k in list(_col_index_cache.keys()):
                if k[0] == ws_key and _col_index_cache[k] >= new_col_idx:
                    _col_index_cache[k] += 1
            _col_index_cache[cache_key] = new_col_idx
            logger.info('Inserted column "%s" after "%s" at index %d in worksheet %s',
                        header_name, after_header, new_col_idx, ws.title)
            return new_col_idx
        # after_header не найден — падаем во fallback (append справа)

    # Append в первую свободную справа
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
    # ДО2 НЕ на Cargo — это per-HAWB поле (HouseWaybill.svh_do2_send_at),
    # отдельный writeback batch_write_svh_do2_dates_for_hawbs.
    # Не делаем early-return при пустых значениях — функция должна
    # уметь ОЧИЩАТЬ Sheets-ячейки если данные были откачены на стороне БД.

    # Дата для Sheets — формат дд.мм.гггг чч:мм:сс по МСК. _local_date_str
    # переводит из UTC если datetime aware.
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
            col_lic      = _ensure_named_column(ws, source.header_row,
                                                CARGOTRACK_SVH_LICENSE_HEADER)
            col_date     = _ensure_named_column(ws, source.header_row,
                                                CARGOTRACK_SVH_DATE_HEADER)
            col_do1      = _ensure_named_column(ws, source.header_row,
                                                CARGOTRACK_SVH_DO1_HEADER)
        except gspread.exceptions.APIError as e:
            logger.exception('svh writeback: ensure column failed: %s', e)
            continue

        # Читаем существующие значения колонок одним запросом каждая,
        # чтобы не писать совпадающее (Google биллит каждый write).
        try:
            existing_lic      = ws.col_values(col_lic)
            existing_date     = ws.col_values(col_date)
            existing_do1      = ws.col_values(col_do1)
        except gspread.exceptions.APIError as e:
            logger.exception('svh writeback: col_values failed: %s', e)
            continue

        letter_lic      = _col_letter(col_lic)
        letter_date     = _col_letter(col_date)
        letter_do1      = _col_letter(col_do1)

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

        updates = _filter_inrange_updates(updates, ws, source.name)
        if not updates:
            continue

        # Bьём на чанки + retry на 503/429 (см. _chunked_batch_update).
        total_writes += _chunked_batch_update(
            ws, updates, f'svh Cargo {cargo.awb_number}', source.name)

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
            ws = _retry_api(open_worksheet, source, label='batch svh open')
            col_lic = _retry_api(_ensure_named_column, ws, source.header_row,
                                 CARGOTRACK_SVH_LICENSE_HEADER,
                                 label='batch svh col_lic')
            col_date = _retry_api(_ensure_named_column, ws, source.header_row,
                                  CARGOTRACK_SVH_DATE_HEADER,
                                  label='batch svh col_date')
            col_do1 = _retry_api(_ensure_named_column, ws, source.header_row,
                                 CARGOTRACK_SVH_DO1_HEADER,
                                 label='batch svh col_do1')
        except (SheetsConfigError, gspread.exceptions.APIError) as e:
            logger.exception('batch svh: open/ensure failed: %s', e)
            continue

        # Читаем все нужные колонки ОДИН РАЗ для всей таблицы
        try:
            existing_lic = _retry_api(ws.col_values, col_lic, label='batch svh read_lic')
            existing_date = _retry_api(ws.col_values, col_date, label='batch svh read_date')
            existing_do1 = _retry_api(ws.col_values, col_do1, label='batch svh read_do1')
        except gspread.exceptions.APIError as e:
            logger.exception('batch svh: col_values failed: %s', e)
            continue

        letter_lic      = _col_letter(col_lic)
        letter_date     = _col_letter(col_date)
        letter_do1      = _col_letter(col_do1)

        updates = []
        for row_idx, cargo in items:
            lic = (cargo.warehouse_license or '').strip()
            placed_str = _local_date_str(cargo.scan_into_bond)
            do1_reg = (cargo.svh_do1_reg_number or '').strip()
            # NB: чужие лицензии (10005/...) — это легитимные данные с
            # moscow-cargo.com парсера (refresh_moscow_cargo). Не фильтруем —
            # юзер хочет видеть инфу о партиях которые едут в Москва Карго.

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

        # Если ImportedSheetRow ссылается на строки за пределами текущего
        # размера Sheet (юзер удалил строки в Sheets после импорта) —
        # отфильтровываем такие записи, иначе ВЕСЬ batch падает с 400.
        updates = _filter_inrange_updates(updates, ws, source.name)
        if not updates:
            continue

        # Bьём на чанки + retry на 503 — _chunked_batch_update определён
        # ниже, но Python резолвит при вызове, не при импорте функции.
        total += _chunked_batch_update(ws, updates, 'svh', source.name)

    return total


def _read_sheet_hawbs(ws, header_row: int) -> list[str]:
    """Читает колонку «Накладная СДЭК» (GEN_HAWB_NUMBER) — возвращает список
    нормализованных HAWB-номеров. Индекс в списке = row_idx - 1.

    Используется как guard в writeback: перед записью сверяем что в Sheets-ряду
    реально наш HAWB. Если юзер пересортировал/удалил строки без захвата всех
    колонок — наш `source_row_index` указывает на чужой ряд, надо пропустить.

    Возвращает [] если колонки HAWB нет (тогда guard отключен).
    """
    from .mapping import GEN_HAWB_NUMBER, normalize_hawb_number
    try:
        header = ws.row_values(header_row)
    except Exception:
        return []
    if GEN_HAWB_NUMBER not in header:
        return []
    col = header.index(GEN_HAWB_NUMBER) + 1
    try:
        raw = ws.col_values(col)
    except Exception:
        return []
    return [normalize_hawb_number(v) for v in raw]


def _filter_inrange_updates(updates: list, ws, source_name: str) -> list:
    """Отбрасывает updates чьи range за пределами текущего размера worksheet.

    ImportedSheetRow.source_row_index фиксируется на момент импорта. Если юзер
    удалил строки в Sheets — наши индексы становятся стейлом, и batch_update
    падает на первом out-of-bounds range, отменяя ВСЕ запросы.
    Лучше пропустить такие записи (с warning'ом) чем потерять весь батч.
    """
    max_row = ws.row_count
    max_col = ws.col_count
    out = []
    skipped = 0
    for u in updates:
        rng = u.get('range', '')
        # 'Z13073' или 'AC1234' → отделить буквенный префикс и число
        m = re.match(r'^([A-Z]+)(\d+)$', rng)
        if not m:
            out.append(u)
            continue
        col_letters, row_str = m.group(1), m.group(2)
        row_num = int(row_str)
        col_num = 0
        for ch in col_letters:
            col_num = col_num * 26 + (ord(ch) - ord('A') + 1)
        if row_num > max_row or col_num > max_col:
            skipped += 1
            continue
        out.append(u)
    if skipped:
        logger.warning('Skipped %d out-of-grid updates in %s '
                       '(max_row=%d, max_col=%d) — Sheet shrunk after import?',
                       skipped, source_name, max_row, max_col)
    return out


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
                            header_name: str, log_label: str,
                            after_header: str = '',
                            formatter=None,
                            value_provider=None) -> int:
    """Generic batch writeback per-HAWB значения — 1 col_values + 1 batch_update.

    Используется для filed_date, release_date, svh_do2_send_at, svh_do1_weight,
    svh_do1_places. value_attr — имя атрибута HAWB-объекта, header_name —
    колонка в Sheets, log_label — префикс для логов, after_header — куда
    вставить колонку при первом создании. formatter — функция (value)→str
    для конвертации значения в строку для Sheets. По умолчанию _local_date_str
    (для datetime → 'дд.мм.гггг чч:мм:сс').
    value_provider — необязательная функция (HAWB)→value, переопределяет
    getattr(h, value_attr). Нужно для случаев когда значение собирается из
    нескольких полей (например MAWB → cargo.awb_number через mawb_id FK).
    """
    if formatter is None:
        formatter = _local_date_str
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
            ws = _retry_api(open_worksheet, source,
                            label=f'batch {log_label} open')
            col = _retry_api(_ensure_named_column, ws, source.header_row,
                             header_name, after_header=after_header,
                             label=f'batch {log_label} ensure_col')
        except (SheetsConfigError, gspread.exceptions.APIError) as e:
            logger.exception('batch %s: open/ensure failed: %s', log_label, e)
            continue

        try:
            existing = _retry_api(ws.col_values, col,
                                  label=f'batch {log_label} read')
        except gspread.exceptions.APIError as e:
            logger.exception('batch %s: col_values failed: %s', log_label, e)
            continue

        letter = _col_letter(col)
        updates = []
        for row_idx, h in items:
            if value_provider is not None:
                value = value_provider(h)
            else:
                value = getattr(h, value_attr)
            value_str = formatter(value)
            cur = (existing[row_idx - 1]
                   if row_idx - 1 < len(existing) else '').strip()
            if cur != value_str:
                updates.append({'range': f'{letter}{row_idx}',
                                'values': [[value_str]]})

        if not updates:
            continue

        updates = _filter_inrange_updates(updates, ws, source.name)
        if not updates:
            continue

        wrote = _chunked_batch_update(ws, updates, log_label, source.name)
        total += wrote

    return total


# Размер чанка для batch_update. Google официально допускает до 10к ячеек,
# но на больших запросах (>2000) часто отдаёт 503 — особенно при нагрузке.
# 500 — безопасный compromise между throughput и стабильностью.
_BATCH_CHUNK_SIZE = 500


def _retry_api(fn, *args, retries=5, label='', **kwargs):
    """Вызывает gspread-функцию с retry на 429/500/502/503/504.

    Нужно для устойчивости к временным сбоям Google Sheets API — без
    этого даже простой row_values/col_values/open_worksheet может упасть
    в нестабильное время и сорвать всю операцию resync. backoff
    экспоненциальный: 2,4,8,16,32 секунды.
    """
    backoff = [2, 4, 8, 16, 32]
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(e.response, 'status_code', None)
            if status in (429, 500, 502, 503, 504) and attempt < retries:
                wait = backoff[min(attempt, len(backoff) - 1)]
                logger.warning('%s API %s, retry in %ds (attempt %d/%d)',
                               label, status, wait, attempt + 1, retries)
                time.sleep(wait)
                continue
            raise


def _chunked_batch_update(ws, updates: list, log_label: str,
                          source_name: str) -> int:
    """Бьёт updates на чанки по _BATCH_CHUNK_SIZE и шлёт каждый отдельно.

    Возвращает суммарное число успешно записанных ячеек. На 503 — retry
    с backoff. На 429 тоже. Прочие APIError → log + skip этого чанка.

    Зачем chunking: Google нестабильно обрабатывает batch >2000 ячеек,
    регулярно отдаёт 503. Маленькие батчи проходят надёжно.
    """
    wrote = 0
    backoff_steps = [2, 4, 8, 16, 32]
    for i in range(0, len(updates), _BATCH_CHUNK_SIZE):
        chunk = updates[i:i + _BATCH_CHUNK_SIZE]
        for attempt in range(len(backoff_steps) + 1):
            try:
                # RAW — Sheets хранит наши строки буквально (см. комментарий
                # выше про USER_ENTERED и формат даты).
                ws.batch_update(chunk, value_input_option='RAW')
                wrote += len(chunk)
                logger.info('batch %s: wrote %d cells in %s (chunk %d/%d)',
                            log_label, len(chunk), source_name,
                            i // _BATCH_CHUNK_SIZE + 1,
                            (len(updates) - 1) // _BATCH_CHUNK_SIZE + 1)
                break
            except gspread.exceptions.APIError as e:
                status = getattr(e.response, 'status_code', None)
                if status in (429, 500, 502, 503, 504) and attempt < len(backoff_steps):
                    wait = backoff_steps[attempt]
                    logger.warning('batch %s %s, retry in %ds (chunk %d/%d)',
                                   log_label, status, wait,
                                   i // _BATCH_CHUNK_SIZE + 1,
                                   (len(updates) - 1) // _BATCH_CHUNK_SIZE + 1)
                    time.sleep(wait)
                    continue
                logger.exception('batch %s: chunk %d failed (status=%s)',
                                 log_label, i // _BATCH_CHUNK_SIZE + 1, status)
                break
    return wrote


def batch_write_filed_dates_for_hawbs(hawbs: list) -> int:
    """Batch writeback filed_date — для resync_filed_dates."""
    return _batch_write_hawb_dates(hawbs, 'filed_date',
                                   CARGOTRACK_FILED_DATE_HEADER, 'filed_date')


def batch_write_release_dates_for_hawbs(hawbs: list) -> int:
    """Batch writeback release_date — для resync_release_dates."""
    return _batch_write_hawb_dates(hawbs, 'release_date',
                                   CARGOTRACK_RELEASE_DATE_HEADER, 'release_date')


def batch_write_svh_do2_dates_for_hawbs(hawbs: list) -> int:
    """Batch writeback svh_do2_send_at — для resync ДО2.

    Колонка вставляется СРАЗУ после «дата выпуска» (юзер просил этот порядок).
    Для HAWB у которых svh_do2_send_at пуст — пишем '' (очищаем стейл).
    """
    return _batch_write_hawb_dates(
        hawbs, 'svh_do2_send_at',
        CARGOTRACK_SVH_DO2_DATE_HEADER, 'svh_do2_date',
        after_header=CARGOTRACK_RELEASE_DATE_HEADER,
    )


def _format_weight(value) -> str:
    """Decimal → '0.062' (без trailing zeros), None → ''."""
    if value is None:
        return ''
    # normalize убирает trailing zeros, str — обычное представление
    from decimal import Decimal
    try:
        d = Decimal(value).normalize()
        # Decimal('0.062').normalize() = Decimal('0.062'); но Decimal('1.0').normalize() = Decimal('1')
        return str(d) if d != 0 else '0'
    except Exception:
        return str(value)


def _format_int(value) -> str:
    """Integer → '1', None → ''."""
    if value is None:
        return ''
    return str(value)


def batch_write_svh_do1_weight_for_hawbs(hawbs: list) -> int:
    """Batch writeback svh_do1_gross_weight в Sheets «вес ДО1»."""
    return _batch_write_hawb_dates(
        hawbs, 'svh_do1_gross_weight',
        CARGOTRACK_SVH_DO1_WEIGHT_HEADER, 'svh_do1_weight',
        after_header=CARGOTRACK_SVH_DO1_HEADER,
        formatter=_format_weight,
    )


def batch_write_svh_do1_places_for_hawbs(hawbs: list) -> int:
    """Batch writeback svh_do1_place_count в Sheets «мест ДО1»."""
    return _batch_write_hawb_dates(
        hawbs, 'svh_do1_place_count',
        CARGOTRACK_SVH_DO1_PLACES_HEADER, 'svh_do1_places',
        after_header=CARGOTRACK_SVH_DO1_WEIGHT_HEADER,
        formatter=_format_int,
    )


def batch_write_svh_do1_sent_for_hawbs(hawbs: list) -> int:
    """Batch writeback HouseWaybill.svh_do1_sent_at в «дата подачи ДО1».

    Per-HAWB — только тем накладным что упомянуты в parsed_meta['hawbs']
    конкретного ED.DO1. Одна партия может иметь несколько ДО-1 с разными
    списками HAWB (например 222 + 186 = 408 на одну MAWB) — поэтому
    cargo-level подход не подходит.
    """
    return _batch_write_hawb_dates(
        hawbs, 'svh_do1_sent_at',
        CARGOTRACK_SVH_DO1_SENT_HEADER, 'svh_do1_sent',
        after_header=CARGOTRACK_SVH_LICENSE_HEADER,
    )


def _mawb_value_provider(h) -> str:
    """Достаёт MAWB-номер из HouseWaybill через mawb FK. '' если без партии."""
    if not h.mawb_id:
        return ''
    return (h.mawb.awb_number or '') if h.mawb else ''


def _identity_str(v) -> str:
    """Formatter: пропускает строку как есть."""
    return v or ''


def batch_write_cargo_mawb_for_hawbs(hawbs: list) -> int:
    """Batch writeback HouseWaybill.mawb.awb_number в «номер партии».

    В Sheets «Общее» MAWB-колонки у юзера нет — мы вынесли её отдельно
    как CargoTrack-колонку. Заполняется автоматически из ED.DO1 outbox
    observations (см. outbox._link_hawbs_to_cargo).
    """
    return _batch_write_hawb_dates(
        hawbs, 'mawb_id',
        CARGOTRACK_CARGO_MAWB_HEADER, 'cargo_mawb',
        formatter=_identity_str,
        value_provider=_mawb_value_provider,
    )


# Одноразовое переименование заголовков — для сохранения данных в существующих
# колонках при смене семантики имени. Ключ = старое имя, значение = новое.
# Применяется на reparse в начале resync (rename_legacy_headers).
LEGACY_HEADER_RENAMES = {
    # «дата ДО1» теперь = «дата регистрации ДО1» (момент когда таможня
    # зарегистрировала ДО-1 = scan_into_bond из CMN.13010). А «дата подачи ДО1»
    # стала отдельной колонкой (момент когда МЫ отправили ДО-1).
    'CargoTrack: дата ДО1': 'CargoTrack: дата регистрации ДО1',
}


def rename_legacy_headers() -> int:
    """Переименовывает заголовки на месте — сохраняет данные в столбце."""
    if not LEGACY_HEADER_RENAMES:
        return 0
    total = 0
    for source in SheetSource.objects.filter(kind='general'):
        try:
            ws = open_worksheet(source)
        except Exception:
            logger.exception('rename_legacy_headers: open failed for %s', source.name)
            continue
        try:
            header_values = ws.row_values(source.header_row)
        except Exception:
            logger.exception('rename_legacy_headers: row_values failed')
            continue
        for idx, val in enumerate(header_values, start=1):
            old = (val or '').strip()
            new = LEGACY_HEADER_RENAMES.get(old)
            if not new:
                continue
            try:
                ws.update_cell(source.header_row, idx, new)
                logger.info('Renamed header "%s" → "%s" (col=%d) in %s',
                            old, new, idx, source.name)
                total += 1
                ws_key = f'{ws.spreadsheet.id}:{ws.id}'
                for k in list(_col_index_cache.keys()):
                    if k[0] == ws_key:
                        del _col_index_cache[k]
            except Exception:
                logger.exception('Failed to rename header "%s" in %s',
                                 old, source.name)
    return total


# Колонки которые мы когда-то создавали, но больше не используем.
# Удаляются однократно при reparse через drop_deprecated_columns().
DEPRECATED_COLUMN_HEADERS = (
    'CargoTrack: рег. номер ДО2',  # был, юзер не использует
    # «дата подачи ДО1» — пустая колонка которую мы создали в неправильном
    # месте (после «дата ДО1»). На этом reparse удаляется и пересоздаётся
    # после «лицензия СВХ» — хронологический порядок подача → регистрация.
    'CargoTrack: дата подачи ДО1',
)


def drop_deprecated_columns() -> int:
    """Удаляет из «Общее» все колонки из DEPRECATED_COLUMN_HEADERS.

    Идемпотентно — если колонки нет, ничего не делает. Возвращает кол-во
    удалённых колонок (для логирования в reparse).
    """
    if not DEPRECATED_COLUMN_HEADERS:
        return 0
    sources = SheetSource.objects.filter(kind='general')
    total_dropped = 0
    for source in sources:
        try:
            ws = open_worksheet(source)
        except SheetsConfigError as e:
            logger.warning('drop_deprecated_columns: open failed for %s: %s',
                           source.name, e)
            continue
        except Exception:
            logger.exception('drop_deprecated_columns: open error for %s',
                             source.name)
            continue

        try:
            header_values = ws.row_values(source.header_row)
        except Exception:
            logger.exception('drop_deprecated_columns: row_values failed')
            continue

        # Идём СПРАВА НАЛЕВО — иначе индексы сдвигаются после удаления
        to_drop = []
        for idx, val in enumerate(header_values, start=1):
            if (val or '').strip() in DEPRECATED_COLUMN_HEADERS:
                to_drop.append((idx, val.strip()))
        for idx, name in sorted(to_drop, reverse=True):
            try:
                ws.delete_columns(idx)
                logger.info('Dropped deprecated column "%s" (idx=%d) from %s',
                            name, idx, source.name)
                total_dropped += 1
                # Инвалидируем кеш индексов колонок этого ws
                ws_key = f'{ws.spreadsheet.id}:{ws.id}'
                for k in list(_col_index_cache.keys()):
                    if k[0] == ws_key:
                        del _col_index_cache[k]
            except Exception:
                logger.exception('Failed to drop column "%s" (idx=%d) from %s',
                                 name, idx, source.name)
    return total_dropped


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

        updates = _filter_inrange_updates(updates, ws, source.name)
        if not updates:
            continue

        total += _chunked_batch_update(ws, updates, 'decl', source.name)

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
