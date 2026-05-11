"""HouseWaybill (HAWB) → WayBillExpressIndividual.xsd.

Генерирует индивидуальную накладную при экспресс-перевозке по схеме
ФТС 5.27.0 (namespace urn:customs.ru:...:WayBillExpressIndividual:5.27.0).

Источник данных:
- cargo.models.HouseWaybill — шапка накладной (номер, отправитель, получатель, веса)
- cargo.models.HAWBGood    — товарные позиции
- cargo.models.Cargo (MAWB)— перевозчик, маршрут, рейс
- параметры carrier_*       — данные таможенного представителя из .env

Тип перевозки фиксирован: 3 (авиа), т.к. CargoTrack Pro работает с авиафрахтом.
DocumentModeID="1002018E" — фиксированный идентификатор схемы.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from lxml import etree

from .base import (
    NS_CAT_RU,
    NS_CLT_TRANS_RU,
    NS_RUSCAT_RU,
    add_base_doc_fields,
    decimal_to_str,
    empty_address,
)

NS_WBEI = 'urn:customs.ru:Information:CommercialFinanceDocuments:WayBillExpressIndividual:5.27.0'

NSMAP_WBEI = {
    None: NS_WBEI,
    'cat_ru': NS_CAT_RU,
    'RUScat_ru': NS_RUSCAT_RU,
    'cltTrans_ru': NS_CLT_TRANS_RU,
}

# Соответствие shipment_type HAWB → CustomsModeCode по классификатору ФТС.
# ИМ — импорт (10), ЭК — экспорт (40), полные коды процедур задаются в .env.
CUSTOMS_MODE_BY_SHIPMENT = {
    'IMPORT': '10',  # выпуск для внутреннего потребления
    'EXPORT': '40',  # экспорт
}


def _org_block(parent: etree._Element, tag: str, *, name: str, inn: str = '') -> etree._Element:
    """Создаёт OrganizationBaseType: <Organisation><OrganizationName>...</OrganizationName></Organisation>

    ИНН и прочие "национальные" реквизиты лежат в RFOrganizationFeatures (для РФ),
    RBOrganizationFeatures (для РБ) и т.д. — но они опциональны и со своей структурой.
    Пока обходимся одним OrganizationName, ИНН подключим позже отдельной задачей.
    """
    org = etree.SubElement(parent, tag)
    el = etree.SubElement(org, f'{{{NS_CAT_RU}}}OrganizationName')
    el.text = (name or 'Не указано')[:250]
    return org


def _address_from_parts(
    parent: etree._Element,
    tag: str,
    *,
    country_code: str = 'RU',
    city: str = '',
    street: str = '',
    postal: str = '',
) -> etree._Element:
    """Создаёт <Address> из плоских полей HAWB. Минимально валидный набор."""
    addr = etree.SubElement(parent, tag)
    el = etree.SubElement(addr, f'{{{NS_CAT_RU}}}CountryCode')
    el.text = country_code or 'RU'
    if postal:
        el = etree.SubElement(addr, f'{{{NS_CAT_RU}}}PostalCode')
        el.text = postal[:9]
    if city:
        el = etree.SubElement(addr, f'{{{NS_CAT_RU}}}City')
        el.text = city[:35]
    if street:
        el = etree.SubElement(addr, f'{{{NS_CAT_RU}}}StreetHouse')
        el.text = street[:120]
    return addr


def build(
    hawb,
    *,
    carrier_name: str,
    carrier_cert_number: str,
    carrier_inn: str = '',
    carrier_okpo: str = '',
    carrier_legal_country: str = 'RU',
    carrier_legal_city: str = '',
    carrier_legal_street: str = '',
    carrier_fact_country: str = 'RU',
    carrier_fact_city: str = '',
    carrier_fact_street: str = '',
    departure_name: Optional[str] = None,
    departure_iata: Optional[str] = None,
    delivery_name: Optional[str] = None,
    delivery_iata: Optional[str] = None,
) -> etree._Element:
    """Строит корневой элемент <WayBillExpressIndividual> для одной HAWB.

    Возвращает etree-элемент (НЕ bytes), чтобы можно было обернуть в Envelope
    через alta.envelope.wrap(). Подпись и сохранение — отдельные слои.

    Параметры carrier_* — данные таможенного представителя (CDEK / твоя компания):
    эти поля одинаковые для всех накладных и приходят из конфигурации (.env),
    а не из модели HouseWaybill.

    Параметры departure_*/delivery_* перекрывают данные из MAWB.Flight,
    если HAWB не привязана к рейсу или нужны другие значения.
    """
    root = etree.Element(
        f'{{{NS_WBEI}}}WayBillExpressIndividual',
        attrib={'DocumentModeID': '1002018E'},
        nsmap=NSMAP_WBEI,
    )

    # 1. BaseDocType fields (только DocumentID, остальные поля BaseDocType
    # — для корректировок и МЧД, нам сейчас не нужны)
    add_base_doc_fields(root)

    # 2. WayBillNumber
    el = etree.SubElement(root, f'{{{NS_WBEI}}}WayBillNumber')
    el.text = hawb.hawb_number

    # 3. CurrencyCode (3-буквенный код, RUB/USD/EUR)
    el = etree.SubElement(root, f'{{{NS_WBEI}}}CurrencyCode')
    el.text = hawb.invoice_currency or 'USD'

    # 4. ShipmentType: 3 = авиа (CargoTrack Pro работает с авиафрахтом)
    el = etree.SubElement(root, f'{{{NS_WBEI}}}ShipmentType')
    el.text = '3'

    # 5. InternationalDistribution: 1 = международная рассылка, 0 = нет
    el = etree.SubElement(root, f'{{{NS_WBEI}}}InternationalDistribution')
    el.text = '1'

    # 6. CustomsModeCode — выбирается из shipment_type
    el = etree.SubElement(root, f'{{{NS_WBEI}}}CustomsModeCode')
    el.text = CUSTOMS_MODE_BY_SHIPMENT.get(hawb.shipment_type, '10')

    # 7. Веса. Суммарные с уровня HAWB или агрегированные по товарам
    goods = list(hawb.goods.all())

    net_total = sum((g.weight_net or Decimal(0) for g in goods), Decimal(0))
    if net_total == 0 and hawb.weight:
        net_total = hawb.weight
    if net_total:
        el = etree.SubElement(root, f'{{{NS_WBEI}}}NetWeightTotal')
        el.text = decimal_to_str(net_total, places=3)

    gross_total = sum((g.weight_gross or Decimal(0) for g in goods), Decimal(0))
    if gross_total == 0 and hawb.weight:
        gross_total = hawb.weight
    el = etree.SubElement(root, f'{{{NS_WBEI}}}GrossWeightTotal')
    el.text = decimal_to_str(gross_total or Decimal('0.001'), places=3)

    # 8. Sender — отправитель из shipper_*
    sender = etree.SubElement(root, f'{{{NS_WBEI}}}Sender')
    _org_block(sender, f'{{{NS_WBEI}}}Organisation', name=hawb.shipper_name, inn=hawb.shipper_inn)
    _address_from_parts(
        sender, f'{{{NS_WBEI}}}Address',
        country_code='CN' if hawb.shipment_type == 'IMPORT' else 'RU',
        city=hawb.shipper_city,
        street=hawb.shipper_address,
    )

    # 9. Receiver — получатель из consignee_*
    receiver = etree.SubElement(root, f'{{{NS_WBEI}}}Receiver')
    _org_block(receiver, f'{{{NS_WBEI}}}Organisation', name=hawb.consignee_name, inn=hawb.consignee_inn)
    _address_from_parts(
        receiver, f'{{{NS_WBEI}}}Address',
        country_code='RU' if hawb.shipment_type == 'IMPORT' else 'CN',
        city=hawb.consignee_city,
        street=hawb.consignee_address,
    )

    # 10. Carrier — таможенный представитель (CDEK / твоя компания)
    carrier = etree.SubElement(root, f'{{{NS_WBEI}}}Carrier')
    el = etree.SubElement(carrier, f'{{{NS_WBEI}}}OrganizationName')
    el.text = carrier_name[:250]
    el = etree.SubElement(carrier, f'{{{NS_WBEI}}}CustomsBrokerCertificate')
    el.text = carrier_cert_number
    if carrier_inn:
        el = etree.SubElement(carrier, f'{{{NS_WBEI}}}INN')
        el.text = carrier_inn
    if carrier_okpo:
        el = etree.SubElement(carrier, f'{{{NS_WBEI}}}OKPOID')
        el.text = carrier_okpo
    _address_from_parts(
        carrier, f'{{{NS_WBEI}}}LegalAddress',
        country_code=carrier_legal_country,
        city=carrier_legal_city,
        street=carrier_legal_street,
    )
    _address_from_parts(
        carrier, f'{{{NS_WBEI}}}FactAddress',
        country_code=carrier_fact_country,
        city=carrier_fact_city,
        street=carrier_fact_street,
    )

    # 11. DeparturePoint — данные рейса хранятся прямо в Cargo (mawb.departure_iata и т.д.)
    mawb = hawb.mawb if hawb.mawb_id else None
    dep = etree.SubElement(root, f'{{{NS_WBEI}}}DeparturePoint')
    el = etree.SubElement(dep, f'{{{NS_WBEI}}}Name')
    el.text = departure_name or (mawb.departure_iata if mawb and mawb.departure_iata else 'Не указан')
    iata = departure_iata or (mawb.departure_iata if mawb else '')
    if iata:
        el = etree.SubElement(dep, f'{{{NS_WBEI}}}IATACode')
        el.text = iata[:3]
    empty_address(dep, f'{{{NS_WBEI}}}Address')

    # 12. DeliveryPoint
    deliv = etree.SubElement(root, f'{{{NS_WBEI}}}DeliveryPoint')
    el = etree.SubElement(deliv, f'{{{NS_WBEI}}}Name')
    el.text = delivery_name or (mawb.arrival_iata if mawb and mawb.arrival_iata else 'Не указан')
    iata = delivery_iata or (mawb.arrival_iata if mawb else '')
    if iata:
        el = etree.SubElement(deliv, f'{{{NS_WBEI}}}IATACode')
        el.text = iata[:3]
    empty_address(deliv, f'{{{NS_WBEI}}}Address')

    # 13. Goods — товарные позиции HAWB
    mawb_number = hawb.mawb.awb_number if hawb.mawb_id else hawb.hawb_number

    if not goods:
        # XSD требует maxOccurs="unbounded" minOccurs по умолчанию 1 — без товаров не пройдёт.
        # Создаём заглушку из шапки HAWB (description + общий вес).
        goods_fake = etree.SubElement(root, f'{{{NS_WBEI}}}Goods')
        etree.SubElement(goods_fake, f'{{{NS_WBEI}}}CommonWayBillNumber').text = mawb_number
        etree.SubElement(goods_fake, f'{{{NS_WBEI}}}Name').text = (
            hawb.description or 'Товары экспресс-отправки'
        )[:250]
        etree.SubElement(goods_fake, f'{{{NS_WBEI}}}GrossWeight').text = decimal_to_str(
            hawb.weight or Decimal('0.001'), places=3
        )
        etree.SubElement(goods_fake, f'{{{NS_WBEI}}}CustomsCost').text = decimal_to_str(
            hawb.invoice_value or Decimal('0'), places=2
        )
        rcv_org = etree.SubElement(goods_fake, f'{{{NS_WBEI}}}ReceiverByCommonWayBill')
        etree.SubElement(rcv_org, f'{{{NS_CAT_RU}}}OrganizationName').text = (
            hawb.consignee_name or 'Не указан'
        )[:250]
    else:
        for g in goods:
            goods_el = etree.SubElement(root, f'{{{NS_WBEI}}}Goods')
            etree.SubElement(goods_el, f'{{{NS_WBEI}}}CommonWayBillNumber').text = mawb_number
            etree.SubElement(goods_el, f'{{{NS_WBEI}}}Name').text = g.name[:250]
            if g.tnved_code:
                etree.SubElement(goods_el, f'{{{NS_WBEI}}}TNVED').text = g.tnved_code
            if g.weight_net:
                etree.SubElement(goods_el, f'{{{NS_WBEI}}}NetWeight').text = decimal_to_str(g.weight_net, places=3)
            etree.SubElement(goods_el, f'{{{NS_WBEI}}}GrossWeight').text = decimal_to_str(
                g.weight_gross or g.weight_net or Decimal('0.001'), places=3
            )
            if g.total_value:
                etree.SubElement(goods_el, f'{{{NS_WBEI}}}InvoicedCost').text = decimal_to_str(g.total_value)
            etree.SubElement(goods_el, f'{{{NS_WBEI}}}CustomsCost').text = decimal_to_str(
                g.total_value or Decimal('0'), places=2
            )
            rcv_org = etree.SubElement(goods_el, f'{{{NS_WBEI}}}ReceiverByCommonWayBill')
            etree.SubElement(rcv_org, f'{{{NS_CAT_RU}}}OrganizationName').text = (
                hawb.consignee_name or 'Не указан'
            )[:250]

    return root
