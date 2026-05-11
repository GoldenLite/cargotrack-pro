"""SOAP-Envelope ФТС для упаковки ЭД-документов.

Каждый ЭД-документ (декларация, накладная, инвойс, вложение) ФТС
требует завернуть в общий Envelope-конверт с маршрутной информацией
и заголовком ЭД (тип сообщения, ID процесса, код получающей таможни).

В hot-folder Альта-ГТД мы кладём ИМЕННО завернутый Envelope —
Альта-Подпись подписывает Body, СВД-Клиент шлёт оператору ЭД.

В случае, когда несколько ЭД относятся к одному пакету
(декларация + инвойс + вложения), у них общий ProccessID.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from lxml import etree

# Namespace'ы ФТС — менять нельзя
NS_SOAP = 'http://www.w3.org/2001/06/soap-envelope'
NS_EDH = 'urn:customs.ru:Envelope:EDHeader:2.0'
NS_ROI = 'urn:customs.ru:Envelope:RoutingInf:1.0'
NS_API = 'urn:customs.ru:Envelope:ApplicationInf:1.0'
NS_ATT = 'urn:customs.ru:Envelope:Attachments:1.0'

NSMAP = {
    None: NS_SOAP,
    'edh': NS_EDH,
    'roi': NS_ROI,
    'api': NS_API,
    'att': NS_ATT,
}

# Версия твоего софта — попадает в <api:SoftVersion>
SOFT_VERSION = 'CargoTrackPro/0.1'


def wrap(
    *,
    body_element: etree._Element,
    message_type: str,
    participant_id: str,
    receiver_customs_code: str,
    process_id: Optional[str] = None,
    envelope_id: Optional[str] = None,
    sender: str = 'smtp://eps.customs.ru/test',
    receiver: str = 'smtp://eps.customs.ru/gateway',
    exch_type: str = '19200',
) -> bytes:
    """Заворачивает готовый body_element в Envelope.

    Args:
        body_element:       уже сформированный корневой элемент ЭД
                            (например, <WayBillExpressIndividual>)
        message_type:       тип ЭД-сообщения, например 'ED.11009'
        participant_id:     ОГРН/ИНН участника ВЭД
        receiver_customs_code: 8-значный код таможни-получателя
        process_id:         UUID бизнес-процесса (общий для всех ЭД пакета).
                            Если не задан — генерируется новый.
        envelope_id:        UUID этого Envelope. Если не задан — генерируется.

    Returns:
        bytes: готовый Envelope в utf-8 с XML-декларацией.
    """
    process_id = process_id or str(uuid.uuid4())
    envelope_id = envelope_id or str(uuid.uuid4())
    now_iso = datetime.now().astimezone().isoformat(timespec='seconds')

    envelope = etree.Element(f'{{{NS_SOAP}}}Envelope', nsmap=NSMAP)

    header = etree.SubElement(envelope, f'{{{NS_SOAP}}}Header')

    routing = etree.SubElement(header, f'{{{NS_ROI}}}RoutingInf')
    etree.SubElement(routing, f'{{{NS_ROI}}}EnvelopeID').text = envelope_id
    etree.SubElement(routing, f'{{{NS_ROI}}}SenderInformation').text = sender
    etree.SubElement(routing, f'{{{NS_ROI}}}ReceiverInformation').text = receiver
    etree.SubElement(routing, f'{{{NS_ROI}}}PreparationDateTime').text = now_iso

    app_inf = etree.SubElement(header, f'{{{NS_API}}}ApplicationInf')
    etree.SubElement(app_inf, f'{{{NS_API}}}SoftVersion').text = SOFT_VERSION

    ed_header = etree.SubElement(header, f'{{{NS_EDH}}}EDHeader')
    etree.SubElement(ed_header, f'{{{NS_EDH}}}MessageType').text = message_type
    etree.SubElement(ed_header, f'{{{NS_EDH}}}ProccessID').text = process_id
    etree.SubElement(ed_header, f'{{{NS_EDH}}}ParticipantID').text = participant_id
    receiver_el = etree.SubElement(ed_header, f'{{{NS_EDH}}}ReceiverCustoms')
    etree.SubElement(receiver_el, f'{{{NS_EDH}}}CustomsCode').text = receiver_customs_code
    etree.SubElement(receiver_el, f'{{{NS_EDH}}}ExchType').text = exch_type

    body = etree.SubElement(envelope, f'{{{NS_SOAP}}}Body')
    body.append(body_element)

    return etree.tostring(
        envelope,
        xml_declaration=True,
        encoding='UTF-8',
        pretty_print=True,
        standalone=False,
    )
