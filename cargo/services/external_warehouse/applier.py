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
import re
from typing import Optional

from django.db import transaction

from cargo.models import Cargo


logger = logging.getLogger('cargo.external.moscow_cargo')

# Префиксы AWB которые приходят в Москва-Карго (Шереметьево).
# Грузы с этими префиксами размещаются на их СВХ; остальные — на наших.
MOSCOW_CARGO_PREFIXES = ('784', '555', '826', '537', '880')

# Классический IATA MAWB: XXX-XXXXXXXX (3 цифры, дефис, 8 цифр).
# Этот regex автоматически отсекает moscow-cargo (784/555/826/537/880) и
# обычные авиа-MAWB (Внуково/Шереметьево). Всё что НЕ совпало — кандидат
# на ДВ (коносамент SNKO*, ESN*, CMR YILI-*, экспресс CDEK-*, и т.п.).
_CLASSIC_MAWB_RE = re.compile(r'^\d{3}-\d{8}$')


def is_moscow_cargo_candidate(cargo: Cargo) -> bool:
    """Подходит ли партия для проверки на Москва-Карго."""
    awb = (cargo.awb_number or '').strip()
    if len(awb) < 4 or awb[3] != '-':
        return False
    return awb[:3] in MOSCOW_CARGO_PREFIXES


def is_far_east_candidate(cargo: Cargo) -> bool:
    """Подходит ли партия для проверки на Декларант Плюс (Дальний Восток).

    True если awb_number НЕ совпадает с классическим IATA MAWB-форматом
    `XXX-XXXXXXXX`. Этот regex автоматически отсекает все moscow_cargo
    префиксы и обычные авиа-MAWB Внуково/Шереметьево.

    Дополнительные фильтры (shipment_type='IMPORT', stage, svh_source)
    применяются в driver-query cron-команды sync_deklarant_svh, не здесь.
    """
    awb = (cargo.awb_number or '').strip().upper()
    if not awb or len(awb) < 4:
        return False
    if _CLASSIC_MAWB_RE.match(awb):
        return False
    return True


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

    # Время размещения. Provider может вернуть:
    # - do1_datetime: полный ISO с реальным временем регистрации (deklarant)
    # - do1_date: только дата YYYY-MM-DD (moscow-cargo)
    # Предпочитаем do1_datetime когда есть.
    if not cargo.scan_into_bond:
        from django.utils.dateparse import parse_datetime
        dt_str = (parsed.get('do1_datetime') or '').strip()
        date_str = (parsed.get('do1_date') or '').strip()
        scan_value = None
        if dt_str:
            parsed_dt = parse_datetime(dt_str)
            if parsed_dt:
                if tz.is_naive(parsed_dt):
                    # ISO без TZ: трактуем как локальную МСК — так Декларант
                    # отдаёт время регистрации ДО1 (Europe/Moscow по умолчанию
                    # для прибалтийских/московских складов; ДВ-склады могут
                    # отдавать +07/+10, но в наблюдаемых ответах TZ был не
                    # указан и время совпадало с МСК-видом портала).
                    parsed_dt = tz.make_aware(parsed_dt)
                scan_value = parsed_dt
        if scan_value is None and date_str:
            d = parse_date(date_str)
            if d:
                # Гибрид: точная дата из moscow-cargo, время = момент обнаружения
                # парсером. Если do1_date = сегодня — используем tz.now()
                # (близко к реальности при свежем размещении). Если раньше —
                # 12:00 как нейтральное значение.
                now = tz.now()
                today_local = tz.localtime(now).date()
                if d == today_local:
                    scan_value = now
                else:
                    scan_value = tz.make_aware(datetime.combine(d, dt_time(12, 0)))
        if scan_value is not None:
            cargo.scan_into_bond = scan_value
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


def fetch_and_apply_deklarant(
    cargo: Cargo,
    client=None,
    *,
    writeback: bool = False,
) -> Optional[bool]:
    """Один шаг: fetch + apply для одной Cargo через Декларант Плюс.

    Возвращает True/False/None:
    - True  — данные пришли и что-то записали (svh_source='deklarant' проставлен)
    - False — данные пришли но всё было уже заполнено (нечего писать)
    - None  — на нашем складе ничего не нашли / ошибка сети
              (DeklarantAuthError пробрасывается наверх — caller mark_dead + abort loop).

    Особенности:
    - Source-precedence guard: если cargo.svh_source != '' и != 'deklarant'
      (alta / moscow_cargo / manual уже заполнили) — пропускаем без HTTP.
    - apply-поля и svh_source сохраняются атомарно (transaction.atomic).
    - writeback=False по умолчанию — caller (cron) делает batch в конце.
    """
    from cargo.services.external_warehouse.deklarant import (
        DeklarantClient, DeklarantAuthError,
    )

    # Защита от перетирания данных другого источника
    if cargo.svh_source and cargo.svh_source not in ('', 'deklarant'):
        logger.debug('deklarant: skip %s, svh_source=%s already set',
                     cargo.awb_number, cargo.svh_source)
        return None

    close_after = False
    if client is None:
        client = DeklarantClient.from_db()
        if not client:
            return None
        close_after = True
    try:
        # DeklarantAuthError пробрасывается из fetch — НЕ глотаем здесь,
        # caller обязан mark_dead session + abort.
        parsed = client.fetch(cargo.awb_number)
        if not parsed:
            return None

        with transaction.atomic():
            changed = apply_to_cargo(cargo, parsed, writeback=False)
            if changed and cargo.svh_source != 'deklarant':
                cargo.svh_source = 'deklarant'
                _save_with_retry(cargo, ['svh_source'])

        if changed and writeback:
            # Single-cargo writeback (для ручного fetch_deklarant --apply).
            # В batch-cron используется sync_deklarant_svh → один общий
            # batch_write_svh_for_cargos после loop.
            import threading

            def _bg_writeback():
                try:
                    from cargo.services.sheets.writeback import (
                        write_svh_placement_for_cargo,
                    )
                    write_svh_placement_for_cargo(cargo)
                except Exception:
                    logger.exception('deklarant sheets writeback failed for %s',
                                     cargo.awb_number)
            threading.Thread(target=_bg_writeback, daemon=True).start()

        return changed
    finally:
        if close_after:
            client.close()
