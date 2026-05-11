"""Постановка документов в очередь на отправку в hot-folder Альты.

Один enqueue() на все 4 типа документов — собирает XML соответствующим
генератором и создаёт запись AltaQueueItem со статусом pending. Сторонний
агент (alta_agent.py) опрашивает API, забирает байты и кладёт их в hot-folder.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from django.contrib.auth.models import User

from . import envelope
from .generators import (
    express_declaration,
    goods_declaration,
    indpost,
    invoice,
    waybill_individual,
)


def _safe(name: str, fallback: str) -> str:
    s = (name or fallback).strip()
    for ch in '/\\:*?"<>|':
        s = s.replace(ch, '_')
    return s or fallback


def _ts() -> str:
    return datetime.now().strftime('%Y%m%d_%H%M%S')


def _carrier_kwargs() -> dict:
    return {
        'carrier_name':        os.environ.get('ALTA_CARRIER_NAME', 'ТЕСТ-ПЕРЕВОЗЧИК'),
        'carrier_cert_number': os.environ.get('ALTA_CARRIER_CERT', '0000/00'),
        'carrier_inn':         os.environ.get('ALTA_CARRIER_INN', ''),
        'carrier_okpo':        os.environ.get('ALTA_CARRIER_OKPO', ''),
        'carrier_legal_city':  os.environ.get('ALTA_CARRIER_CITY', ''),
        'carrier_legal_street': os.environ.get('ALTA_CARRIER_STREET', ''),
        'carrier_fact_city':   os.environ.get('ALTA_CARRIER_CITY', ''),
        'carrier_fact_street': os.environ.get('ALTA_CARRIER_STREET', ''),
    }


def _wrap(body, message_type: str) -> bytes:
    return envelope.wrap(
        body_element=body,
        message_type=message_type,
        participant_id=os.environ.get('ALTA_PARTICIPANT_ID', '0000000000000'),
        receiver_customs_code=os.environ.get('ALTA_CUSTOMS_CODE', '10005030'),
    )


def enqueue_indpost(hawb, *, user: Optional[User] = None):
    from cargo.models import AltaQueueItem
    content = indpost.build(
        hawb,
        customs_code=os.environ.get('ALTA_CUSTOMS_CODE', ''),
        origin_country=os.environ.get('ALTA_DEFAULT_ORIGIN_COUNTRY', 'CN'),
    )
    filename = f'IndPost_{_safe(hawb.hawb_number, "hawb")}_{_ts()}.xml'
    return AltaQueueItem.objects.create(
        doc_type='indpost', hawb=hawb, cargo=hawb.mawb,
        filename=filename, content=content, content_encoding='windows-1251',
        created_by=user,
    )


def enqueue_waybill(hawb, *, user: Optional[User] = None):
    from cargo.models import AltaQueueItem
    body = waybill_individual.build(hawb, **_carrier_kwargs())
    content = _wrap(body, 'ED.1002018')
    filename = f'WayBill_{_safe(hawb.hawb_number, "hawb")}_{_ts()}.xml'
    return AltaQueueItem.objects.create(
        doc_type='waybill', hawb=hawb, cargo=hawb.mawb,
        filename=filename, content=content, content_encoding='utf-8',
        created_by=user,
    )


def enqueue_invoice(hawb, *, user: Optional[User] = None):
    from cargo.models import AltaQueueItem
    body = invoice.build(hawb)
    content = _wrap(body, 'ED.1002007')
    filename = f'Invoice_{_safe(hawb.hawb_number, "hawb")}_{_ts()}.xml'
    return AltaQueueItem.objects.create(
        doc_type='invoice', hawb=hawb, cargo=hawb.mawb,
        filename=filename, content=content, content_encoding='utf-8',
        created_by=user,
    )


def enqueue_express(cargo, *, user: Optional[User] = None):
    from cargo.models import AltaQueueItem
    body = express_declaration.build(cargo)
    content = _wrap(body, 'ED.1006275')
    filename = f'ExpressDecl_{_safe(cargo.awb_number, "cargo")}_{_ts()}.xml'
    return AltaQueueItem.objects.create(
        doc_type='express', cargo=cargo,
        filename=filename, content=content, content_encoding='utf-8',
        created_by=user,
    )


def enqueue_dt(cargo, *, user: Optional[User] = None):
    from cargo.models import AltaQueueItem
    body = goods_declaration.build(
        cargo,
        declarant_name=os.environ.get('ALTA_CARRIER_NAME', ''),
        declarant_inn=os.environ.get('ALTA_CARRIER_INN', ''),
    )
    content = _wrap(body, 'ED.1006107')
    filename = f'DT_{_safe(cargo.awb_number, "cargo")}_{_ts()}.xml'
    return AltaQueueItem.objects.create(
        doc_type='dt', cargo=cargo,
        filename=filename, content=content, content_encoding='utf-8',
        created_by=user,
    )
