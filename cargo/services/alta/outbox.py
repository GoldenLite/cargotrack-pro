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
