"""Cargo + HAWBs + Goods → ESADout_CU.xsd (классическая ДТ).

Электронная копия декларации на товары (ЭСАД исх.) по схеме ФТС 5.27.0 —
то, что в РФ исторически называли ГТД. DocumentModeID="1006107E".

ВНИМАНИЕ: минимальная заготовка. Реальная ДТ требует заполнения десятков
обязательных полей (54 графы + платежи + декларант + транспорт + контракт).
Эта версия покрывает костяк, валидируется по XSD, но к подаче в ФТС
требует дозаполнения. Расчёт построен на итеративном развитии по
обратной связи XSD-валидатора.
"""
from __future__ import annotations

from datetime import datetime as _dt
from decimal import Decimal

from lxml import etree

from .base import (
    NS_CAT_RU,
    NS_CAT_ESAD_CU,
    NS_CLT_ESAD_CU,
    NS_RUSCAT_RU,
    add_base_doc_fields,
    decimal_to_str,
)

NS_ESAD = 'urn:customs.ru:Information:CustomsDocuments:ESADout_CU:5.27.0'
NS_RUDECLCAT = 'urn:customs.ru:RUDeclCommonAggregateTypesCust:5.27.0'

NSMAP_ESAD = {
    None: NS_ESAD,
    'cat_ru': NS_CAT_RU,
    'catESAD_cu': NS_CAT_ESAD_CU,
    'cltESAD_cu': NS_CLT_ESAD_CU,
    'RUScat_ru': NS_RUSCAT_RU,
    'RUDECLcat': NS_RUDECLCAT,
}

# CustomsProcedure: ИМ/ЭК — гр.1 первый подраздел ДТ
CUSTOMS_PROCEDURE_BY_SHIPMENT = {
    'IMPORT': 'ИМ',
    'EXPORT': 'ЭК',
}

# CustomsModeCode: код процедуры по классификатору — гр.1 второй подраздел
CUSTOMS_MODE_BY_SHIPMENT = {
    'IMPORT': '10',  # выпуск для внутреннего потребления
    'EXPORT': '40',  # экспорт
}


def _add_eec_doc_header(parent: etree._Element) -> None:
    """EECEDocHeaderAddInfo — обязательный заголовок ЭД (тот же что в ДТЭГ)."""
    hdr = etree.SubElement(parent, f'{{{NS_ESAD}}}EECEDocHeaderAddInfo')
    el = etree.SubElement(hdr, f'{{{NS_RUSCAT_RU}}}EDocCode')
    el.text = 'R.107'  # формальный код по pattern R.NNN, реальный код — у ФТС
    el = etree.SubElement(hdr, f'{{{NS_RUSCAT_RU}}}EDocDateTime')
    el.text = _dt.now().strftime('%Y-%m-%dT%H:%M:%S')


def _add_declarant(parent: etree._Element, *, name: str, inn: str = '') -> None:
    """ESADout_CUDeclarant — обязательное поле, декларант товаров.

    AEODeclarantDetailsType — расширение GoodsShipmentSubjectDetailsType.
    Минимум: OrganizationName. Реквизиты по странам — отдельный choice.
    """
    decl = etree.SubElement(parent, f'{{{NS_ESAD}}}ESADout_CUDeclarant')
    el = etree.SubElement(decl, f'{{{NS_CAT_RU}}}OrganizationName')
    el.text = (name or 'Не указано')[:250]


def _add_goods_item(parent: etree._Element, hawb_good, ordinal: int) -> None:
    """ESADout_CUGoods — товарная позиция (гр. 31-47 ДТ).

    Минимально валидный товар: GoodsNumeric (порядковый), Description, ТНВЭД,
    вес, страны происхождения, стоимость.
    """
    g = etree.SubElement(parent, f'{{{NS_ESAD}}}ESADout_CUGoods')

    # Порядковый номер товара
    el = etree.SubElement(g, f'{{{NS_CAT_ESAD_CU}}}GoodsNumeric')
    el.text = str(ordinal)

    # Описание товара
    el = etree.SubElement(g, f'{{{NS_CAT_ESAD_CU}}}GoodsDescription')
    el.text = hawb_good.name[:250]

    # ТНВЭД
    if hawb_good.tnved_code:
        el = etree.SubElement(g, f'{{{NS_CAT_ESAD_CU}}}GoodsTNVEDCode')
        el.text = hawb_good.tnved_code

    # OriginCountryCode — страна происхождения. Дефолт по shipment_type
    # родительской HAWB: для импорта обычно CN, для экспорта — RU.
    origin = 'CN' if hawb_good.hawb.shipment_type == 'IMPORT' else 'RU'
    el = etree.SubElement(g, f'{{{NS_CAT_ESAD_CU}}}OriginCountryCode')
    el.text = origin

    # Веса (NetWeightQuantity/GrossWeightQuantity) в XSD идут глубоко,
    # после десятка других полей. Для минимально валидной ДТ пропускаем —
    # реальная Альта-ГТД редактор сам подсчитает по товарной части.


def build(cargo, *, declarant_name: str = '', declarant_inn: str = '') -> etree._Element:
    """Строит ESADout_CU для всего Cargo + его HAWB + товары.

    Args:
        cargo: объект Cargo
        declarant_name: наименование декларанта (если не задано, берётся
            таможенный представитель из env через carrier_name)
    """
    root = etree.Element(
        f'{{{NS_ESAD}}}ESADout_CU',
        attrib={'DocumentModeID': '1006107E'},
        nsmap=NSMAP_ESAD,
    )

    # 1. BaseDocType.DocumentID
    add_base_doc_fields(root)

    # 2. EECEDocHeaderAddInfo (обязательный)
    _add_eec_doc_header(root)

    # 3. CustomsProcedure (обязательный): ИМ/ЭК — гр.1 первый подраздел
    hawbs = list(cargo.hawbs.all())
    shipment_type = hawbs[0].shipment_type if hawbs else 'IMPORT'
    el = etree.SubElement(root, f'{{{NS_ESAD}}}CustomsProcedure')
    el.text = CUSTOMS_PROCEDURE_BY_SHIPMENT.get(shipment_type, 'ИМ')

    # 4. CustomsModeCode — гр.1 второй подраздел (10 / 40)
    el = etree.SubElement(root, f'{{{NS_ESAD}}}CustomsModeCode')
    el.text = CUSTOMS_MODE_BY_SHIPMENT.get(shipment_type, '10')

    # 5. ElectronicDocumentSign — гр.1 третий подраздел: "ЭД"
    el = etree.SubElement(root, f'{{{NS_ESAD}}}ElectronicDocumentSign')
    el.text = 'ЭД'

    # 6. ESADout_CUGoodsShipment — товарная партия
    gs = etree.SubElement(root, f'{{{NS_ESAD}}}ESADout_CUGoodsShipment')

    # ESADout_CUDeclarant — обязательное поле
    _add_declarant(gs, name=declarant_name or 'Не указано', inn=declarant_inn)

    # ESADout_CUGoods — товары из всех HAWB партии, сквозной нумерацией
    ordinal = 0
    for hawb in hawbs:
        for hawb_good in hawb.goods.all():
            ordinal += 1
            _add_goods_item(gs, hawb_good, ordinal=ordinal)
    if ordinal == 0:
        # XSD требует хотя бы один товар. Заглушка из cargo.
        ordinal = 1
        g = etree.SubElement(gs, f'{{{NS_ESAD}}}ESADout_CUGoods')
        etree.SubElement(g, f'{{{NS_CAT_ESAD_CU}}}GoodsNumeric').text = '1'
        etree.SubElement(g, f'{{{NS_CAT_ESAD_CU}}}GoodsDescription').text = (
            f'Товары партии {cargo.awb_number}'
        )[:250]

    return root
