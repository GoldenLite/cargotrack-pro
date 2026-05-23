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


def apply_to_cargo(cargo: Cargo, parsed: dict) -> bool:
    """Заполняет пустые СВХ-поля Cargo из parsed-данных moscow-cargo.

    Возвращает True если что-то реально записали (тогда писать в Sheets имеет
    смысл).
    """
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

    cargo.save(update_fields=updated)
    logger.info('moscow-cargo applied to %s: %s', cargo.awb_number, updated)

    # Триггер writeback в Sheets (те же 3 колонки что для Альты-СВХ)
    try:
        from cargo.services.sheets.writeback import write_svh_placement_for_cargo
        write_svh_placement_for_cargo(cargo)
    except Exception:
        logger.exception('moscow-cargo sheets writeback failed for %s', cargo.awb_number)

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
