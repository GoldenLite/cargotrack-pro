"""Realtime CRM-вкладки writeback из dispatch.

ПОЧЕМУ: «Общее» обновляется за 30-60 сек после release-сообщения, потому
что dispatch напрямую вызывает batch_write_*_for_hawbs() (модуль writeback).
CRM-вкладки специалистов обновлялись только через crm_sync_incremental
(отдельный cron, 5 мин + время прогона + риск зависания на 429-волне).
Лаг для юзера = 5-15 минут, иногда 2+ часа.

Этот модуль исправляет архитектурный недостаток: предоставляет
batch_write_*_for_crm_hawbs() аналоги, которые dispatch вызывает рядом
с «Общее»-writeback'ом. crm_sync_incremental остаётся safety-net.

ОТНОШЕНИЕ К crm_sync_incremental:
- Realtime пишет ТОЧЕЧНО (одна-две HAWB за раз) → дёшево по API quota.
- Также обновляет CrmHawbIndex.last_* → cron не видит diff'а и не пишет
  повторно.
- Hide/show остаётся за crm_sync_incremental (требует batchUpdate request
  на ss-level + сортирует строки — не realtime-операция).
- Если realtime упал (network/auth) — cron в течение 5 мин догонит.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Iterable

import gspread.exceptions

from cargo.models import CrmHawbIndex
from cargo.services.sheets.client import get_client


logger = logging.getLogger('cargo.crm_realtime')


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'

# Колонки CRM-вкладок (см. crm_sync_incremental.py).
COL_HAWB         = 3   # C
COL_ARRIVAL_DATE = 5   # E
COL_WAREHOUSE    = 6   # F
COL_T            = 20  # T (checkbox «подано/в работе/выпущено»)
COL_REQUEST      = 21  # U
COL_DECL         = 23  # W
COL_ED_STATUS    = 24  # X


def _col_letter(n: int) -> str:
    out = ''
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _retry(fn, *args, label: str = '', max_retries: int = 4, **kwargs):
    """Локальный retry на 429/5xx с коротким backoff (realtime — не ждём долго).
    На сетевых сбоях — exit без падения caller'а."""
    import requests.exceptions as _rex
    import urllib3.exceptions as _u3ex
    import ssl as _ssl
    backoff = [1, 2, 4, 8]
    for attempt in range(min(max_retries, len(backoff)) + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(e.response, 'status_code', None)
            if status in (429, 500, 502, 503, 504) and attempt < max_retries:
                wait = backoff[min(attempt, len(backoff) - 1)]
                logger.warning('crm_rt %s API %s, retry in %ds',
                               label, status, wait)
                time.sleep(wait)
                continue
            raise
        except (_rex.ConnectionError, _rex.Timeout,
                _u3ex.MaxRetryError, _ssl.SSLError, OSError) as e:
            if attempt < max_retries:
                wait = backoff[min(attempt, len(backoff) - 1)]
                logger.warning('crm_rt %s network %s, retry in %ds',
                               label, type(e).__name__, wait)
                time.sleep(wait)
                continue
            raise


def _compute_want_decl(new_decl: str, new_status: str, last_decl: str) -> str:
    """Правило feedback_decl_only_on_released:
    - 'Выпуск разрешен' (substring-match) → пишем decl
    - пустой ed_status → НЕ трогаем (legacy строки)
    - любой иной (Отказ, Отзыв, Запросы и т.п.) → стираем
    """
    if 'Выпуск разрешен' in (new_status or ''):
        return new_decl or ''
    if not new_status:
        return last_decl
    return ''


def _hawbs_to_crm_index_groups(hawbs: Iterable) -> dict[str, list]:
    """Группирует CrmHawbIndex по tab_name для batched-update."""
    hawb_numbers = [h.hawb_number for h in hawbs if getattr(h, 'hawb_number', '')]
    if not hawb_numbers:
        return {}
    by_tab: dict[str, list] = defaultdict(list)
    for entry in CrmHawbIndex.objects.filter(hawb_number__in=hawb_numbers):
        by_tab[entry.tab_name].append(entry)
    return by_tab


def _open_ss_and_ws_map():
    """Открывает spreadsheet и возвращает (ss, {title: ws})."""
    client = get_client()
    ss = client.open_by_key(CRM_ID)
    ws_by_title = {ws.title: ws for ws in ss.worksheets()}
    return ss, ws_by_title


def live_row_map(ws) -> dict:
    """{hawb_number → 1-based row} из ЖИВОЙ колонки C (COL_HAWB) вкладки.

    Sort-proof таргетинг: пишем/скрываем в строку, где HAWB РЕАЛЬНО сейчас,
    а не по кэшу CrmHawbIndex.row_index. Это позволяет менеджерам свободно
    резать/вставлять/двигать строки в Google Sheets — запись подстраивается,
    в чужую строку не пишем.

    Читается заново на каждый прогон (без TTL-кэша) — чтобы сразу после
    ручного редактирования не было окна со stale-позициями.
    """
    try:
        col = _retry(ws.col_values, COL_HAWB, label=f'live rows {ws.title}')
    except Exception:
        logger.exception('crm_rt: live col read failed for %r', ws.title)
        return {}
    m: dict[str, int] = {}
    for i, v in enumerate(col):
        s = str(v or '').strip()
        if s and s not in m:   # первая встреча = верхняя строка (дубли игнорим)
            m[s] = i + 1
    return m


def resolve_live_rows(ws_by_title, entries_by_tab) -> dict:
    """{(tab, hawb_number) → live_row} по живой колонке C для каждой вкладки.

    Если HAWB больше нет на вкладке — ключа нет (caller пропускает запись,
    не трогая чужую строку). Один read колонки C на вкладку.
    """
    out: dict = {}
    for tab, entries in entries_by_tab.items():
        ws = ws_by_title.get(tab)
        if ws is None:
            continue
        lm = live_row_map(ws)
        for e in entries:
            r = lm.get(e.hawb_number)
            if r:
                out[(tab, e.hawb_number)] = r
    return out


def _write_batch(ws_by_title, updates_per_tab: dict, label: str) -> int:
    """Пишет batched updates per-tab. Возвращает суммарное число cells."""
    wrote = 0
    for tab, updates in updates_per_tab.items():
        if not updates:
            continue
        ws = ws_by_title.get(tab)
        if not ws:
            logger.warning('crm_rt %s: tab %r not in spreadsheet', label, tab)
            continue
        try:
            _retry(ws.batch_update, updates,
                   value_input_option='USER_ENTERED',
                   label=f'{label}/{tab}')
            wrote += len(updates)
            logger.debug('crm_rt %s: wrote %d cells in %r',
                         label, len(updates), tab)
        except Exception:
            logger.exception('crm_rt %s: batch_update failed for %r',
                             label, tab)
    return wrote


def batch_write_ed_status_for_crm_hawbs(hawbs: list, ws_by_title: dict = None,
                                        live: dict = None) -> int:
    """Realtime запись «Статус ЭД» (колонка X) в CRM-вкладках специалистов.

    Параллельно с «Общее»-writeback. Если HAWB не в CrmHawbIndex (нет в
    спец-вкладках) — no-op. Обновляет CrmHawbIndex.last_status чтобы cron
    не дёргался впустую.

    sort-proof: пишем в ЖИВУЮ строку HAWB (по колонке C), а не по кэшу
    row_index — менеджеры могут свободно двигать строки. ws_by_title/live
    можно передать снаружи (batch_write_all переиспользует на 3 функции),
    иначе открываем/резолвим сами."""
    from cargo.services.alta.ed_status import compute_ed_status

    if not hawbs:
        return 0

    by_tab = _hawbs_to_crm_index_groups(hawbs)
    if not by_tab:
        return 0

    # Считаем новые значения per-HAWB (тяжёлая часть — compute_ed_status).
    # Function-scoped батч-кэш: создаётся после DB-мутаций вызывающего,
    # умирает на выходе — убирает per-HAWB raw_xml-LIKE без риска стейла.
    from cargo.services.alta.ed_status import ed_status_batch
    new_status_by_num = {}
    hawbs_by_num = {h.hawb_number: h for h in hawbs if getattr(h, 'hawb_number', '')}
    with ed_status_batch():
        for hn, h in hawbs_by_num.items():
            try:
                new_status_by_num[hn] = compute_ed_status(h)
            except Exception:
                logger.exception('crm_rt ed_status compute for %s failed', hn)
                new_status_by_num[hn] = None

    # Открываем лист ПЕРВЫМ — нужно прочитать живую колонку C (sort-proof).
    own_open = ws_by_title is None
    if own_open:
        try:
            _, ws_by_title = _open_ss_and_ws_map()
        except Exception:
            logger.exception('crm_rt ed_status: spreadsheet open failed')
            return 0
    if live is None:
        live = resolve_live_rows(ws_by_title, by_tab)

    updates_per_tab: dict[str, list] = defaultdict(list)
    idx_to_save: list[CrmHawbIndex] = []
    for tab, entries in by_tab.items():
        for entry in entries:
            new_status = new_status_by_num.get(entry.hawb_number)
            if new_status is None:
                continue
            if new_status == entry.last_status:
                continue
            row = live.get((tab, entry.hawb_number))
            if row is None:
                continue  # HAWB больше нет на этой вкладке — в чужую строку не пишем
            updates_per_tab[tab].append({
                'range': f'{_col_letter(COL_ED_STATUS)}{row}',
                'values': [[new_status]],
            })
            entry.last_status = (new_status or '')[:128]
            if entry.row_index != row:
                entry.row_index = row  # self-heal индекса к реальной строке
            idx_to_save.append(entry)

    if not updates_per_tab:
        return 0

    wrote = _write_batch(ws_by_title, updates_per_tab, 'ed_status')

    if idx_to_save:
        try:
            from django.utils import timezone as djtz
            now = djtz.now()
            for e in idx_to_save:
                e.last_synced_at = now
            CrmHawbIndex.objects.bulk_update(
                idx_to_save,
                fields=['last_status', 'last_synced_at', 'row_index'],
                batch_size=500,
            )
        except Exception:
            logger.exception('crm_rt ed_status: index bulk_update failed')

    return wrote


def batch_write_decl_for_crm_hawbs(hawbs: list, ws_by_title: dict = None,
                                   live: dict = None) -> int:
    """Realtime запись «Регистрационный номер ДТ» (колонка W) в CRM-вкладках.
    Правило feedback_decl_only_on_released — пишем decl ТОЛЬКО при статусе
    «Выпуск разрешен», при не-released — стираем.

    sort-proof: пишем в живую строку HAWB (колонка C)."""
    from cargo.services.alta.ed_status import compute_ed_status

    if not hawbs:
        return 0

    by_tab = _hawbs_to_crm_index_groups(hawbs)
    if not by_tab:
        return 0

    from cargo.services.alta.ed_status import ed_status_batch
    hawbs_by_num = {h.hawb_number: h for h in hawbs if getattr(h, 'hawb_number', '')}
    snapshot = {}
    with ed_status_batch():
        for hn, h in hawbs_by_num.items():
            try:
                new_status = compute_ed_status(h)
            except Exception:
                new_status = ''
            new_decl = (getattr(h, 'customs_declaration_number', '') or '').strip()
            snapshot[hn] = (new_decl, new_status)

    own_open = ws_by_title is None
    if own_open:
        try:
            _, ws_by_title = _open_ss_and_ws_map()
        except Exception:
            logger.exception('crm_rt decl: spreadsheet open failed')
            return 0
    if live is None:
        live = resolve_live_rows(ws_by_title, by_tab)

    updates_per_tab: dict[str, list] = defaultdict(list)
    idx_to_save: list[CrmHawbIndex] = []
    for tab, entries in by_tab.items():
        for entry in entries:
            new_decl, new_status = snapshot.get(entry.hawb_number, (None, None))
            if new_decl is None:
                continue
            want = _compute_want_decl(new_decl, new_status, entry.last_decl)
            if want == entry.last_decl:
                continue
            row = live.get((tab, entry.hawb_number))
            if row is None:
                continue  # HAWB больше нет на этой вкладке — в чужую строку не пишем
            updates_per_tab[tab].append({
                'range': f'{_col_letter(COL_DECL)}{row}',
                'values': [[want]],
            })
            entry.last_decl = (want or '')[:64]
            if entry.row_index != row:
                entry.row_index = row  # self-heal индекса
            idx_to_save.append(entry)

    if not updates_per_tab:
        return 0

    wrote = _write_batch(ws_by_title, updates_per_tab, 'decl')

    if idx_to_save:
        try:
            from django.utils import timezone as djtz
            now = djtz.now()
            for e in idx_to_save:
                e.last_synced_at = now
            CrmHawbIndex.objects.bulk_update(
                idx_to_save,
                fields=['last_decl', 'last_synced_at', 'row_index'],
                batch_size=500,
            )
        except Exception:
            logger.exception('crm_rt decl: index bulk_update failed')

    return wrote


def batch_write_request_for_crm_hawbs(hawbs: list, ws_by_title: dict = None,
                                      live: dict = None) -> int:
    """Realtime «Запросы таможни» (колонка U). Пишем только если новое
    значение непустое и отличается.

    sort-proof: пишем в живую строку HAWB (колонка C)."""
    from cargo.services.sheets.writeback import _customs_requests_text

    if not hawbs:
        return 0
    by_tab = _hawbs_to_crm_index_groups(hawbs)
    if not by_tab:
        return 0

    hawbs_by_num = {h.hawb_number: h for h in hawbs if getattr(h, 'hawb_number', '')}
    snapshot = {}
    for hn, h in hawbs_by_num.items():
        try:
            snapshot[hn] = _customs_requests_text(h)
        except Exception:
            snapshot[hn] = ''

    own_open = ws_by_title is None
    if own_open:
        try:
            _, ws_by_title = _open_ss_and_ws_map()
        except Exception:
            logger.exception('crm_rt request: spreadsheet open failed')
            return 0
    if live is None:
        live = resolve_live_rows(ws_by_title, by_tab)

    updates_per_tab: dict[str, list] = defaultdict(list)
    idx_to_save: list[CrmHawbIndex] = []
    for tab, entries in by_tab.items():
        for entry in entries:
            new_request = snapshot.get(entry.hawb_number, '')
            if not new_request or new_request == entry.last_request:
                continue
            row = live.get((tab, entry.hawb_number))
            if row is None:
                continue  # HAWB больше нет на этой вкладке — в чужую строку не пишем
            updates_per_tab[tab].append({
                'range': f'{_col_letter(COL_REQUEST)}{row}',
                'values': [[new_request]],
            })
            entry.last_request = new_request
            if entry.row_index != row:
                entry.row_index = row  # self-heal индекса
            idx_to_save.append(entry)

    if not updates_per_tab:
        return 0
    wrote = _write_batch(ws_by_title, updates_per_tab, 'request')

    if idx_to_save:
        try:
            from django.utils import timezone as djtz
            now = djtz.now()
            for e in idx_to_save:
                e.last_synced_at = now
            CrmHawbIndex.objects.bulk_update(
                idx_to_save,
                fields=['last_request', 'last_synced_at', 'row_index'],
                batch_size=500,
            )
        except Exception:
            logger.exception('crm_rt request: index bulk_update failed')
    return wrote


def batch_write_all_for_crm_hawbs(hawbs: list) -> dict[str, int]:
    """Удобный wrapper: ed_status + decl + request за один вызов.
    Обёрнут в try/except — никакой fail не должен ломать caller (dispatch).

    Возвращает {'ed_status': N, 'decl': N, 'request': N}."""
    result = {'ed_status': 0, 'decl': 0, 'request': 0}
    if not hawbs:
        return result
    # Открываем лист и резолвим живые строки ОДИН раз на 3 функции
    # (sort-proof без 3× чтений колонки C). Если не вышло — функции
    # откроют/резолвят сами (own_open).
    ws_by_title = live = None
    try:
        by_tab = _hawbs_to_crm_index_groups(hawbs)
        if by_tab:
            _, ws_by_title = _open_ss_and_ws_map()
            live = resolve_live_rows(ws_by_title, by_tab)
    except Exception:
        logger.exception('crm_rt: open/resolve failed; под-функции откроют сами')
        ws_by_title = live = None
    try:
        result['ed_status'] = batch_write_ed_status_for_crm_hawbs(
            hawbs, ws_by_title, live)
    except Exception:
        logger.exception('crm_rt: ed_status writeback failed')
    try:
        result['decl'] = batch_write_decl_for_crm_hawbs(hawbs, ws_by_title, live)
    except Exception:
        logger.exception('crm_rt: decl writeback failed')
    try:
        result['request'] = batch_write_request_for_crm_hawbs(
            hawbs, ws_by_title, live)
    except Exception:
        logger.exception('crm_rt: request writeback failed')
    return result
