"""Запись данных внешнего СВХ (Москва-Карго) в Cargo + writeback в Sheets.

Семантика:
- ДО1 на чужом СВХ для нас = единственный источник СВХ-данных по этим партиям
  (от нашей Альты-СВХ мы по партиям префиксов 784/555/826/537/880 не получаем
  CMN.13010 — это не наш склад). Поэтому конфликта с CMN.13010 нет.
- НЕ перезаписываем заполненные поля Cargo. Если ручной ввод или Альта-СВХ
  что-то уже поставила — moscow-cargo не трогает.
- Sheets writeback использует те же 3 колонки что для Альты:
  «лицензия СВХ», «дата ДО1», «рег. номер ДО1». Юзеру неважно откуда —
  важна информация.
"""
from __future__ import annotations

import logging
from typing import Optional

from cargo.models import Cargo


logger = logging.getLogger('cargo.external.moscow_cargo')

# Префиксы AWB которые приходят в Москва-Карго (Шереметьево).
# Грузы с этими префиксами размещаются на их СВХ; остальные — на наших.
MOSCOW_CARGO_PREFIXES = ('784', '555', '826', '537', '880')


def is_moscow_cargo_candidate(cargo: Cargo) -> bool:
    """Подходит ли партия для проверки на Москва-Карго."""
    awb = (cargo.awb_number or '').strip()
    if len(awb) < 4 or awb[3] != '-':
        return False
    return awb[:3] in MOSCOW_CARGO_PREFIXES


def _save_with_retry(cargo: Cargo, fields: list[str], attempts: int = 5) -> None:
    """save(update_fields=) с retry на 'database is locked'.

    SQLite WAL терпит несколько писателей, но Sheets-writeback держит DB-сессию
    долго (HTTP в Google) и может перекрыть наш save. PRAGMA busy_timeout
    (5000ms) обычно справляется, но при гонке с import_sheets cron окно может
    превысить лимит. Простой backoff закрывает кейс без архитектурных правок.
    """
    import time as _time
    from django.db import OperationalError

    delay = 1.0
    for i in range(attempts):
        try:
            cargo.save(update_fields=fields)
            return
        except OperationalError as e:
            if 'locked' not in str(e).lower() or i == attempts - 1:
                raise
            logger.warning('moscow-cargo save locked for %s, retry in %.1fs',
                           cargo.awb_number, delay)
            _time.sleep(delay)
            delay *= 2


def apply_to_cargo(cargo: Cargo, parsed: dict, *, writeback: bool = True) -> bool:
    """Заполняет пустые СВХ-поля Cargo из parsed-данных moscow-cargo.

    Возвращает True если что-то реально записали.

    Параметр `writeback=False` используется батч-сценариями (например
    refresh_moscow_cargo) — там Sheets API ограничен 300 read/min, поэтому
    дешевле собрать ВСЕ изменённые Cargo и сделать один общий resync,
    чем делать per-cargo writeback (=~ 4 API-вызова на штуку).
    """
    import threading
    from datetime import datetime, time as dt_time
    from django.utils.dateparse import parse_date
    from django.utils import timezone as tz

    if not parsed:
        return False

    updated: list[str] = []

    license_ = parsed.get('license') or ''
    if license_ and not (cargo.warehouse_license or '').strip():
        cargo.warehouse_license = license_
        updated.append('warehouse_license')

    reg = parsed.get('reg_number') or ''
    if reg and not (cargo.svh_do1_reg_number or '').strip():
        cargo.svh_do1_reg_number = reg
        updated.append('svh_do1_reg_number')

    date_str = parsed.get('do1_date') or ''
    if date_str and not cargo.scan_into_bond:
        d = parse_date(date_str)
        if d:
            # Time приходит как 00:00 — Sheets всё равно показывает только дату.
            cargo.scan_into_bond = tz.make_aware(datetime.combine(d, dt_time(0, 0)))
            updated.append('scan_into_bond')

    if not updated:
        return False

    _save_with_retry(cargo, updated)
    logger.info('moscow-cargo applied to %s: %s', cargo.awb_number, updated)

    if not writeback:
        return True

    # Single-cargo сценарий (signal post_save, fetch_moscow_cargo --apply) —
    # writeback в фоновом потоке.
    def _bg_writeback():
        try:
            from cargo.services.sheets.writeback import write_svh_placement_for_cargo
            write_svh_placement_for_cargo(cargo)
        except Exception:
            logger.exception('moscow-cargo sheets writeback failed for %s',
                             cargo.awb_number)
    threading.Thread(target=_bg_writeback, daemon=True).start()

    return True


def fetch_and_apply(cargo: Cargo,
                    client=None) -> Optional[bool]:
    """Один шаг: fetch + apply для одной Cargo.

    Возвращает True/False/None:
    - True  — данные пришли и что-то записали
    - False — данные пришли но всё было уже заполнено (ничего не писали)
    - None  — на сайте ничего не нашли (или ошибка сети, не upgrade)
    """
    from .moscow_cargo import MoscowCargoClient

    close_after = False
    if client is None:
        client = MoscowCargoClient()
        close_after = True
    try:
        parsed = client.fetch(cargo.awb_number)
        if not parsed:
            return None
        return apply_to_cargo(cargo, parsed)
    finally:
        if close_after:
            client.close()
