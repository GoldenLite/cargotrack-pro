"""Cargo + HAWBs → ExpressCargoDeclaration.xsd.

Декларация на товары/пассажирская таможенная декларация для экспресс-грузов
(ДТЭГ/ПТДЭГ) по схеме ФТС 5.27.0.

Источник данных:
- cargo.models.Cargo — MAWB (партия), общие реквизиты декларации
- cargo.models.HouseWaybill[] — индивидуальные накладные внутри партии
- cargo.models.HAWBGood[] — товары внутри каждой HAWB

В этой схеме обязательны:
    EECEDocHeaderAddInfo, DocType, ExpressRegistryKindCode, ElectronicDocumentSign

Остальное опционально, но без GoodsShipment декларация бессмысленна.
DocumentModeID="1006275E" — фиксированный идентификатор схемы.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from lxml import etree

from .base import (
    NS_CAT_RU,
    NS_CLT_ESAD_CU,
    NS_RUSCAT_RU,
    add_base_doc_fields,
    decimal_to_str,
)

NS_ECD = 'urn:customs.ru:Information:CustomsDocuments:ExpressCargoDeclaration:5.27.0'
NS_RUDECLCAT = 'urn:customs.ru:RUDeclCommonAggregateTypesCust:5.27.0'

NSMAP_ECD = {
    None: NS_ECD,
    'cat_ru': NS_CAT_RU,
    'RUScat_ru': NS_RUSCAT_RU,
    'RUDECLcat': NS_RUDECLCAT,
    'cltESAD_cu': NS_CLT_ESAD_CU,
}

# Коды видов декларации по классификатору ФТС
EXPRESS_REGISTRY_KIND_DTEG = 'ДТЭГ'  # Декларация на товары для экспресс-грузов
EXPRESS_REGISTRY_KIND_PTDEG = 'ПТДЭГ'  # Пассажирская там. декл. для экспресс-грузов

# Признак электронного документа (всегда "ЭД")
ELECTRONIC_DOCUMENT_SIGN = 'ЭД'

# DocType: 0 — оригинал ДТЭГ/ПТДЭГ, 1 — корректировка (КДТЭГ/КПТДЭГ)
DOC_TYPE_ORIGINAL = '0'

# DeclarationKindCode — тип декларации (ИМ/ЭК/ЭТ)
DECLARATION_KIND_BY_SHIPMENT = {
    'IMPORT': 'ИМ',
    'EXPORT': 'ЭК',
}


def _add_eec_doc_header(parent: etree._Element, cargo) -> None:
    """EECEDocHeaderAddInfo — обязательный заголовок ЭД.

    Обязательные поля EECEDocHeaderAddInfoType (по XSD):
        EDocCode      — код документа в реестре структур ЭД
        EDocDateTime  — дата/время создания
    """
    from datetime import datetime as _dt

    hdr = etree.SubElement(parent, f'{{{NS_ECD}}}EECEDocHeaderAddInfo')
    # EDocCode — кодовое обозначение документа по реестру структур ЭД.
    # Pattern: R(\.[A-Z]{2}\.[A-Z]{2}\.[0-9]{2})?\.[0-9]{3}
    # Минимальный валидный формат — R.NNN, реальное значение
    # ФТС присвоит сама / возьмёт из своего реестра при импорте в Альту.
    el = etree.SubElement(hdr, f'{{{NS_RUSCAT_RU}}}EDocCode')
    el.text = 'R.275'
    el = etree.SubElement(hdr, f'{{{NS_RUSCAT_RU}}}EDocDateTime')
    el.text = _dt.now().strftime('%Y-%m-%dT%H:%M:%S')


def _house_shipment(parent: etree._Element, hawb, ordinal: int) -> etree._Element:
    """HouseShipment — товарная партия по одной индивидуальной накладной (HAWB)."""
    hs = etree.SubElement(parent, f'{{{NS_ECD}}}HouseShipment')

    # WayBillID — уникальный UUID этой партии в декларации (не путать с номером HAWB)
    import uuid as _uuid
    el = etree.SubElement(hs, f'{{{NS_ECD}}}WayBillID')
    el.text = str(_uuid.uuid4()).upper()

    # ObjectOrdinal — порядковый номер в декларации
    el = etree.SubElement(hs, f'{{{NS_ECD}}}ObjectOrdinal')
    el.text = str(ordinal)

    # HouseWaybillDetails — реквизиты HAWB (номер, дата)
    hwd = etree.SubElement(hs, f'{{{NS_ECD}}}HouseWaybillDetails')
    el = etree.SubElement(hwd, f'{{{NS_CAT_RU}}}PrDocumentNumber')
    el.text = hawb.hawb_number
    el = etree.SubElement(hwd, f'{{{NS_CAT_RU}}}PrDocumentDate')
    el.text = hawb.created_at.date().strftime('%Y-%m-%d')

    # Общая таможенная стоимость по HAWB
    if hawb.invoice_value:
        cc = etree.SubElement(hs, f'{{{NS_ECD}}}CustomsCost')
        amount = etree.SubElement(cc, f'{{{NS_ECD}}}CurrencyQuantity')
        amount.text = decimal_to_str(hawb.invoice_value)
        curr = etree.SubElement(cc, f'{{{NS_ECD}}}CurrencyCode')
        curr.text = hawb.invoice_currency or 'USD'

    return hs


def build(cargo) -> etree._Element:
    """Строит ExpressCargoDeclaration для всего Cargo (MAWB) + его HAWBs.

    На вход: объект Cargo. Берёт все привязанные HAWB через `cargo.hawbs.all()`,
    каждую разворачивает в HouseShipment.

    Возвращает etree._Element (НЕ bytes), чтобы можно было обернуть в Envelope.
    """
    root = etree.Element(
        f'{{{NS_ECD}}}ExpressCargoDeclaration',
        attrib={'DocumentModeID': '1006275E'},
        nsmap=NSMAP_ECD,
    )

    # 1. BaseDocType.DocumentID (UUID)
    add_base_doc_fields(root)

    # 2. EECEDocHeaderAddInfo (обязательный)
    _add_eec_doc_header(root, cargo)

    # 3. DocType: 0 — оригинал
    el = etree.SubElement(root, f'{{{NS_ECD}}}DocType')
    el.text = DOC_TYPE_ORIGINAL

    # 4. ExpressRegistryKindCode: ДТЭГ (для экспресс-грузов с товарами B2B/B2C)
    el = etree.SubElement(root, f'{{{NS_ECD}}}ExpressRegistryKindCode')
    el.text = EXPRESS_REGISTRY_KIND_DTEG

    # 5. DeclarationKindCode (опц., но полезно): ИМ/ЭК
    # Берём из первой HAWB — внутри Cargo все обычно одного типа отправки
    hawbs = list(cargo.hawbs.all())
    if hawbs:
        shipment_type = hawbs[0].shipment_type
        kind = DECLARATION_KIND_BY_SHIPMENT.get(shipment_type)
        if kind:
            el = etree.SubElement(root, f'{{{NS_ECD}}}DeclarationKindCode')
            el.text = kind

    # 6. ElectronicDocumentSign (обязательный): "ЭД"
    el = etree.SubElement(root, f'{{{NS_ECD}}}ElectronicDocumentSign')
    el.text = ELECTRONIC_DOCUMENT_SIGN

    # 7. GoodsShipment — массив HouseShipment по числу HAWB
    if hawbs:
        gs = etree.SubElement(root, f'{{{NS_ECD}}}GoodsShipment')
        for idx, hawb in enumerate(hawbs, start=1):
            _house_shipment(gs, hawb, ordinal=idx)

        # Общий вес и стоимость по декларации
        total_gross = sum(
            (h.weight or Decimal(0) for h in hawbs), Decimal(0)
        )
        if total_gross:
            uw = etree.SubElement(gs, f'{{{NS_ECD}}}UnifiedGrossWeightQuantity')
            # SupplementaryQuantityType: GoodsQuantity (обяз.) + опц. имя/код ед.
            el = etree.SubElement(uw, f'{{{NS_CAT_RU}}}GoodsQuantity')
            el.text = decimal_to_str(total_gross, places=3)
            el = etree.SubElement(uw, f'{{{NS_CAT_RU}}}MeasureUnitQualifierCode')
            el.text = '166'  # кг

        total_value = sum(
            (h.invoice_value or Decimal(0) for h in hawbs), Decimal(0)
        )
        if total_value:
            cc = etree.SubElement(gs, f'{{{NS_ECD}}}CustomsCost')
            amount = etree.SubElement(cc, f'{{{NS_ECD}}}CurrencyQuantity')
            amount.text = decimal_to_str(total_value)
            curr = etree.SubElement(cc, f'{{{NS_ECD}}}CurrencyCode')
            curr.text = hawbs[0].invoice_currency or 'USD'

    return root
