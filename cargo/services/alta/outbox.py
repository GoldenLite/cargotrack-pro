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
    """Привязывает наблюдение к Cargo и/или HAWB по вытащенным номерам."""
    cargo = _find_cargo(obs.common_waybill_number)
    hawb  = _find_hawb(obs.waybill_number)
    # Если HAWB найдена, а её mawb совпадает — заодно подтянем cargo.
    if hawb and not cargo and hawb.mawb_id:
        cargo = hawb.mawb
    return cargo, hawb


def dispatch(obs: AltaOutboxObservation) -> None:
    """Линкует obs к Cargo/HAWB. Сохраняет только если хоть что-то нашли."""
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
