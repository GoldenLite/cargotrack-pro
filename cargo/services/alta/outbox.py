"""Outbox observation: исходящие копии Альты, наблюдаемые агентом.

Альта складывает в `C:\\GTDSERV\\ED\\IN` не только входящие ответы таможни
(`serveralta^*.gz`), но и свои собственные исходящие копии (`538134^*.gz`).
Они дают нам единственный способ узнать какой EnvelopeID Альта присвоила
конкретной подаче — этот UUID потом возвращается в `InitialEnvelopeID`
входящих ответов и нужен для матчинга.

`dispatch(obs)` вызывается из view `api_alta_outbox_post` после
`update_or_create`. Делает только линковку obs → (Cargo, HAWB) по
вытащенным агентом полям `common_waybill_number` и `waybill_number`.
Сами входящие ответы матчатся уже в `inbox.match()` через эту таблицу.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from cargo.models import AltaOutboxObservation, Cargo, HouseWaybill


logger = logging.getLogger('cargo.alta.outbox')


def _find_cargo(common_wb: str) -> Optional[Cargo]:
    common_wb = (common_wb or '').strip()
    if not common_wb:
        return None
    return Cargo.objects.filter(awb_number__iexact=common_wb).first()


def _find_hawb(wb: str) -> Optional[HouseWaybill]:
    wb = (wb or '').strip()
    if not wb:
        return None
    return HouseWaybill.objects.filter(hawb_number__iexact=wb).first()


def link(obs: AltaOutboxObservation) -> Tuple[Optional[Cargo], Optional[HouseWaybill]]:
    """Привязывает наблюдение к Cargo и/или HAWB по вытащенным номерам.

    Для msg_type='ED.DO1' (DO1Report от Альты-СВХ) есть fallback: если MAWB
    в common_waybill_number не нашли Cargo — ищем по любому HAWB-номеру
    из parsed_meta['hawbs'] и берём его mawb.
    """
    cargo = _find_cargo(obs.common_waybill_number)
    hawb  = _find_hawb(obs.waybill_number)
    # Если HAWB найдена, а её mawb совпадает — заодно подтянем cargo.
    if hawb and not cargo and hawb.mawb_id:
        cargo = hawb.mawb
    # ED.DO1: fallback через список HAWB партии (parsed_meta['hawbs'])
    if not cargo and obs.msg_type == 'ED.DO1':
        for hawb_num in (obs.parsed_meta or {}).get('hawbs') or []:
            h = _find_hawb(hawb_num)
            if h and h.mawb_id:
                cargo = h.mawb
                break
    return cargo, hawb


def dispatch(obs: AltaOutboxObservation) -> None:
    """Линкует obs к Cargo/HAWB. Сохраняет только если хоть что-то нашли.

    Для msg_type='ED.DO1' дополнительно проставляет Cargo.svh_do1_sent_at =
    obs.prepared_at (момент когда Альта-СВХ отправила ДО-1 в таможню).
    """
    cargo, hawb = link(obs)
    update_fields = []
    if cargo and obs.cargo_id != cargo.pk:
        obs.cargo = cargo
        update_fields.append('cargo')
    if hawb and obs.hawb_id != hawb.pk:
        obs.hawb = hawb
        update_fields.append('hawb')
    if update_fields:
        obs.save(update_fields=update_fields)
        logger.info('outbox %s linked: cargo=%s hawb=%s', obs.envelope_id,
                    obs.cargo_id, obs.hawb_id)

    # ED.DO1: записать дату подачи ДО-1 в Cargo + триггерить Sheets writeback.
    if obs.msg_type == 'ED.DO1' and cargo and obs.prepared_at:
        if cargo.svh_do1_sent_at != obs.prepared_at:
            Cargo.objects.filter(pk=cargo.pk).update(
                svh_do1_sent_at=obs.prepared_at)
            logger.info('ED.DO1: Cargo %s svh_do1_sent_at = %s',
                        cargo.awb_number, obs.prepared_at)
            _writeback_svh_do1_sent(cargo)

    # ED.DO1: per-HAWB вес и места из <Goods> блоков ДО-1. parsed_meta может
    # содержать goods{hawb: {weight, places}} (если agent послал raw_xml,
    # endpoint распарсил через parse_do1_report).
    if obs.msg_type == 'ED.DO1':
        goods = (obs.parsed_meta or {}).get('goods') or {}
        if goods:
            _apply_do1_goods(goods)

    # CMN.11023 (первичная подача ДТ) / CMN.11349 (корректировка) — содержат
    # точный момент подачи в таможню. Заполняем HouseWaybill.filed_date с
    # реальным временем (раньше там стояло 00:00:00 из CMN-RegistrationDate).
    # Приоритет: 11023 > 11349 (если оба есть, берём первое). Логика:
    # пишем только если поле пустое ИЛИ новое prepared_at раньше.
    if obs.msg_type in ('CMN.11023', 'CMN.11349') and hawb and obs.prepared_at:
        _maybe_update_filed_date(hawb, obs.prepared_at)


def _maybe_update_filed_date(hawb: HouseWaybill, prepared_at) -> None:
    """Обновляет HouseWaybill.filed_date если новое значение раньше или поле пустое.

    Берёт самую раннюю подачу — CMN.11023 (первая) приоритетнее CMN.11349
    (корректировка), но в БД они могут попадать в любом порядке. Через
    сравнение prepared_at выбираем именно самое раннее.
    """
    hawb.refresh_from_db(fields=['filed_date'])
    if hawb.filed_date and hawb.filed_date <= prepared_at:
        return  # уже стоит более раннее значение
    HouseWaybill.objects.filter(pk=hawb.pk).update(filed_date=prepared_at)
    logger.info('filed_date: HAWB %s set to %s', hawb.hawb_number, prepared_at)
    _writeback_filed_date(hawb)


def _apply_do1_goods(goods: dict) -> None:
    """Записывает per-HAWB вес и места из ДО-1 Goods блоков.

    goods: {'10257142180': {'weight': '0.062', 'places': 1}, ...}
    Идемпотентно — обновляет только если значение изменилось.
    """
    from decimal import Decimal, InvalidOperation
    affected: list[HouseWaybill] = []
    for hawb_num, data in goods.items():
        h = HouseWaybill.objects.filter(hawb_number__iexact=hawb_num).first()
        if not h:
            continue
        try:
            new_weight = Decimal(str(data.get('weight') or '0'))
        except (InvalidOperation, ValueError):
            new_weight = None
        new_places = data.get('places') or None
        update_fields = {}
        if new_weight is not None and h.svh_do1_gross_weight != new_weight:
            update_fields['svh_do1_gross_weight'] = new_weight
        if new_places is not None and h.svh_do1_place_count != new_places:
            update_fields['svh_do1_place_count'] = new_places
        if update_fields:
            HouseWaybill.objects.filter(pk=h.pk).update(**update_fields)
            affected.append(h)
    if affected:
        logger.info('ED.DO1 goods: updated %d HAWBs (weight/places)', len(affected))
        try:
            from cargo.services.sheets.writeback import (
                batch_write_svh_do1_weight_for_hawbs,
                batch_write_svh_do1_places_for_hawbs,
                signals_suppressed,
            )
            if not signals_suppressed():
                for h in affected:
                    h.refresh_from_db(fields=['svh_do1_gross_weight', 'svh_do1_place_count'])
                batch_write_svh_do1_weight_for_hawbs(affected)
                batch_write_svh_do1_places_for_hawbs(affected)
        except Exception:
            logger.exception('svh_do1 weight/places writeback failed')


def _writeback_filed_date(hawb: HouseWaybill) -> None:
    """Sync ячейки «дата подачи» в Sheets для одного HAWB."""
    try:
        from cargo.services.sheets.writeback import (
            write_filed_date_for_hawb, signals_suppressed,
        )
        if signals_suppressed():
            return
        hawb.refresh_from_db(fields=['filed_date'])
        write_filed_date_for_hawb(hawb)
    except Exception:
        logger.exception('filed_date writeback failed')


def _writeback_svh_do1_sent(cargo: Cargo) -> None:
    """Sync ячейки «дата подачи ДО1» в Sheets. Skip если signals_suppressed
    (bulk-операция типа reparse сама делает resync в конце).
    """
    try:
        from cargo.services.sheets.writeback import (
            batch_write_svh_do1_sent_for_cargos, signals_suppressed,
        )
        if signals_suppressed():
            return
        cargo.refresh_from_db(fields=['svh_do1_sent_at'])
        batch_write_svh_do1_sent_for_cargos([cargo])
    except Exception:
        logger.exception('svh_do1_sent writeback failed')


def relink_for_cargo(cargo: Cargo) -> int:
    """После появления нового Cargo — допривязать к нему уже накопленные
    AltaOutboxObservation с тем же CommonWayBillNumber. Возвращает счётчик.
    """
    awb = (cargo.awb_number or '').strip()
    if not awb:
        return 0
    return (
        AltaOutboxObservation.objects
        .filter(common_waybill_number__iexact=awb, cargo=None)
        .update(cargo=cargo)
    )


def relink_for_hawb(hawb: HouseWaybill) -> int:
    """Аналогично для HAWB по WayBillNumber."""
    wb = (hawb.hawb_number or '').strip()
    if not wb:
        return 0
    return (
        AltaOutboxObservation.objects
        .filter(waybill_number__iexact=wb, hawb=None)
        .update(hawb=hawb)
    )
