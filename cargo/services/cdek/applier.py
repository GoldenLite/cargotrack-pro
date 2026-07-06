"""Запись статусов доставки СДЭК на HouseWaybill (read-only трекинг).

Принцип (как в external_warehouse/applier.py):
- Возвращаем bool «что-то изменилось».
- НЕ трогаем logistics_status / customs_status — у СДЭК свой неймспейс
  `cdek_*`. Опциональный one-way bump на DELIVERED — только за флагом
  settings.CDEK_AUTO_ADVANCE_DELIVERED (по умолчанию выкл).
- Сводные cdek_* поля HouseWaybill отражают ТЕКУЩИЙ статус; перезаписываем
  их только если входящий статус не старше сохранённого (защита от
  out-of-order доставки вебхуков).
- Каждый статус из истории материализуем в CdekStatusEvent идемпотентно
  (unique по hawb+status_code+occurred_at), как HawbWorkflowEvent.

Матчинг HAWB: im_number заказа СДЭК == HouseWaybill.hawb_number.
"""
from __future__ import annotations

import logging
from typing import Optional, Union

from django.conf import settings
from django.utils import timezone

from cargo.models import CdekStatusEvent, HouseWaybill
from .client import CDEK_TERMINAL_CODES, CdekClient, extract_statuses


logger = logging.getLogger('cargo.cdek.applier')

# Поздние логистические статусы, из которых безопасно авто-продвинуть на
# DELIVERED по факту доставки СДЭК (если включён CDEK_AUTO_ADVANCE_DELIVERED).
_ADVANCE_FROM = (
    'READY_DELIVERY', 'TO_SORT_CENTER', 'AT_SORT_CENTER',
    'READY_TO_DEST', 'IN_TRANSIT_DEST', 'ARRIVED_FINAL',
)


def _parse_cdek_dt(s: str):
    """ISO-строка СДЭК ('2020-08-10T21:32:14+0700') → aware datetime | None."""
    s = (s or '').strip()
    if not s:
        return None
    dt = None
    try:
        from django.utils.dateparse import parse_datetime
        dt = parse_datetime(s)
    except (ValueError, TypeError):
        dt = None
    if dt is None:
        # Фолбэк: fromisoformat понимает и '+0700' (Python 3.11+).
        from datetime import datetime
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def resolve_hawb(im_number: str) -> Optional[HouseWaybill]:
    """HAWB по im_number заказа СДЭК. hawb_number уникален → .first() безопасен."""
    im_number = (im_number or '').strip()
    if not im_number:
        return None
    return HouseWaybill.objects.filter(hawb_number__iexact=im_number).first()


def _save_with_retry(hawb: HouseWaybill, fields: list, attempts: int = 5) -> None:
    """save(update_fields=) с backoff на 'database is locked' (SQLite + вебхуки).

    Вебхук может писать одновременно с auto_sync/Sheets-writeback. WAL +
    busy_timeout обычно покрывают, простой backoff закрывает остаток окна.
    """
    import time as _time
    from django.db import OperationalError

    delay = 0.5
    for i in range(attempts):
        try:
            hawb.save(update_fields=fields)
            return
        except OperationalError as e:
            if 'locked' not in str(e).lower() or i == attempts - 1:
                raise
            logger.warning('cdek save locked for %s, retry in %.1fs',
                           hawb.hawb_number, delay)
            _time.sleep(delay)
            delay *= 2


def apply_status_to_hawb(hawb: HouseWaybill, parsed: dict,
                         *, source: str = 'webhook') -> bool:
    """Применяет статусы СДЭК к HAWB. Возвращает True если что-то изменилось.

    parsed — результат cdek.client.extract_statuses(entity).
    """
    if not hawb or not parsed:
        return False

    changed = False
    uuid = parsed.get('uuid') or ''
    cdek_number = parsed.get('cdek_number') or ''

    # 1. История статусов → CdekStatusEvent (идемпотентно).
    for st in parsed.get('statuses') or []:
        code = st.get('code') or ''
        occurred = _parse_cdek_dt(st.get('date_time') or '')
        if not code or occurred is None:
            continue
        _, created = CdekStatusEvent.objects.update_or_create(
            hawb=hawb,
            status_code=code,
            occurred_at=occurred,
            defaults={
                'status_name': st.get('name') or '',
                'city': st.get('city') or '',
                'cdek_uuid': uuid,
                'cdek_number': cdek_number,
                'source': source,
                'raw': st,
            },
        )
        if created:
            changed = True
            logger.info('cdek event for HAWB %s: %s (%s) @ %s',
                        hawb.hawb_number, code, st.get('name') or '', occurred)

    # 2. Сводные cdek_* поля = текущий статус (с защитой от out-of-order).
    current = parsed.get('current')
    fields: list = []

    if uuid and hawb.cdek_uuid != uuid:
        hawb.cdek_uuid = uuid
        fields.append('cdek_uuid')
    if cdek_number and hawb.cdek_number != cdek_number:
        hawb.cdek_number = cdek_number
        fields.append('cdek_number')

    if current:
        cur_dt = _parse_cdek_dt(current.get('date_time') or '')
        # Перезаписываем сводный статус только если он не старше сохранённого.
        if cur_dt is not None and (
            hawb.cdek_status_date is None or cur_dt >= hawb.cdek_status_date
        ):
            cur_code = current.get('code') or ''
            cur_name = current.get('name') or ''
            if hawb.cdek_status_code != cur_code:
                hawb.cdek_status_code = cur_code
                fields.append('cdek_status_code')
            if hawb.cdek_status_name != cur_name:
                hawb.cdek_status_name = cur_name
                fields.append('cdek_status_name')
            if hawb.cdek_status_date != cur_dt:
                hawb.cdek_status_date = cur_dt
                fields.append('cdek_status_date')

    # cdek_synced_at обновляем всегда когда был успешный fetch/apply.
    hawb.cdek_synced_at = timezone.now()
    fields.append('cdek_synced_at')

    if fields:
        _save_with_retry(hawb, list(dict.fromkeys(fields)))
        if set(fields) - {'cdek_synced_at'}:
            changed = True

    # 3. Опциональный one-way bump логистики на DELIVERED.
    if getattr(settings, 'CDEK_AUTO_ADVANCE_DELIVERED', False):
        _maybe_advance_delivered(hawb, current)

    return changed


def _maybe_advance_delivered(hawb: HouseWaybill, current: Optional[dict]) -> None:
    """Если СДЭК доставил, а HAWB на поздней стадии — продвинуть в DELIVERED.

    Строго one-way и только из «безопасных» поздних статусов. Никогда не
    откатывает DELIVERED/RETURNED/LOST.
    """
    if not current or (current.get('code') or '') != 'DELIVERED':
        return
    if hawb.logistics_status not in _ADVANCE_FROM:
        return
    try:
        err = hawb.change_logistics_status('DELIVERED', user=None,
                                           comment='Авто: СДЭК доставил')
        if err:
            logger.warning('cdek auto-advance DELIVERED отклонён для %s: %s',
                           hawb.hawb_number, err)
        else:
            logger.info('cdek auto-advance: HAWB %s → DELIVERED', hawb.hawb_number)
    except Exception:
        logger.exception('cdek auto-advance DELIVERED failed for %s',
                         hawb.hawb_number)


def fetch_and_apply(hawb_or_im: Union[HouseWaybill, str],
                    client: Optional[CdekClient] = None,
                    *, source: str = 'manual') -> Optional[bool]:
    """fetch заказа из СДЭК по im_number (=hawb_number) + apply.

    Возврат:
    - True  — статус пришёл и что-то записали
    - False — пришёл, но всё уже было актуально
    - None  — заказа в СДЭК нет / ошибка (не апгрейд)
    """
    if isinstance(hawb_or_im, HouseWaybill):
        hawb = hawb_or_im
        im_number = hawb.hawb_number
    else:
        im_number = str(hawb_or_im or '').strip()
        hawb = resolve_hawb(im_number)
        if not hawb:
            logger.info('cdek fetch_and_apply: HAWB %s не найдена', im_number)
            return None

    close_after = False
    if client is None:
        client = CdekClient()
        close_after = True
    try:
        entity = client.get_order_by_im_number(im_number)
        if not entity:
            return None
        parsed = extract_statuses(entity)
        return apply_status_to_hawb(hawb, parsed, source=source)
    finally:
        if close_after:
            client.close()


def is_terminal(status_code: str) -> bool:
    return (status_code or '').strip() in CDEK_TERMINAL_CODES
