"""HouseWaybill + HAWBGood → Invoice.xsd.

Коммерческий инвойс по схеме ФТС 5.24.0. Используется как самостоятельный
документ или как вложение (prDocument) к декларации.

В этой схеме обязательны (без minOccurs="0"):
    CurrencyCode, GCost, TotalCost,
    Buyer (.Name), Seler (.Name),  -- да, в схеме именно "Seler" с одной "l"
    InvoiceGoods (>= 1, каждое с GoodsDescription),
    DeliveryTerms (DeliveryTermsStringCode + 3 кода стран),
    Registration (DocumentBaseType — номер + дата).

DocumentModeID="1002007E" — фиксированный идентификатор схемы.
"""
from __future__ import annotations

from decimal import Decimal

from lxml import etree

from .base import NS_CAT_RU, add_base_doc_fields, decimal_to_str

NS_INV = 'urn:customs.ru:Information:CommercialFinanceDocuments:Invoice:5.24.0'
NS_CAT_COMFIN = 'urn:customs.ru:Information:CommercialFinanceDocuments:CommercialFinanceCommonAgregateTypesCust:5.24.0'

NSMAP_INV = {
    None: NS_INV,
    'cat_ru': NS_CAT_RU,
    'catComFin_ru': NS_CAT_COMFIN,
}

# Дефолтный код Incoterms — FCA (Free Carrier) подходит для экспресс-грузов.
# Можно переопределить через параметр delivery_terms.
DEFAULT_INCOTERMS = 'FCA'


def _add_participant(parent: etree._Element, tag: str, *, name: str, inn: str = '') -> None:
    """Buyer/Seler — InvoiceParticipantInfType (extends InvoiceParticipantType
    из catComFin_ru namespace). Дочерние элементы Name/CompanyID живут в
    namespace БАЗОВОГО типа (catComFin_ru), а не локальной схемы Invoice.
    """
    p = etree.SubElement(parent, tag)
    if inn:
        el = etree.SubElement(p, f'{{{NS_CAT_COMFIN}}}CompanyID')
        el.text = inn
    el = etree.SubElement(p, f'{{{NS_CAT_COMFIN}}}Name')
    el.text = (name or 'Не указано')[:250]


def _add_goods(parent: etree._Element, good) -> None:
    """InvoiceGoods — одна товарная позиция.
    Обязательное поле: GoodsDescription. Остальное — улучшает качество.
    """
    g = etree.SubElement(parent, f'{{{NS_INV}}}InvoiceGoods')

    if good.article:
        el = etree.SubElement(g, f'{{{NS_CAT_COMFIN}}}GoodMarking')
        el.text = good.article[:50]

    if good.tnved_code:
        el = etree.SubElement(g, f'{{{NS_CAT_COMFIN}}}GoodsCode')
        el.text = good.tnved_code

    # GoodsDescription обязателен (минимум один)
    el = etree.SubElement(g, f'{{{NS_CAT_COMFIN}}}GoodsDescription')
    el.text = good.name[:250]

    if good.quantity:
        el = etree.SubElement(g, f'{{{NS_CAT_COMFIN}}}GoodsQuantity')
        el.text = decimal_to_str(good.quantity, places=3)

    if good.unit:
        el = etree.SubElement(g, f'{{{NS_CAT_COMFIN}}}MeasureUnitQualifierName')
        el.text = good.unit[:50]

    # Веса по товару — иначе схема требует хотя бы Price или вес
    if good.weight_gross:
        el = etree.SubElement(g, f'{{{NS_CAT_COMFIN}}}GrossWeightQuantity')
        el.text = decimal_to_str(good.weight_gross, places=3)
    if good.weight_net:
        el = etree.SubElement(g, f'{{{NS_CAT_COMFIN}}}NetWeightQuantity')
        el.text = decimal_to_str(good.weight_net, places=3)

    # Price — цена за единицу
    if good.unit_price:
        el = etree.SubElement(g, f'{{{NS_CAT_COMFIN}}}Price')
        el.text = decimal_to_str(good.unit_price)
    elif good.total_value and good.quantity:
        el = etree.SubElement(g, f'{{{NS_CAT_COMFIN}}}Price')
        el.text = decimal_to_str(good.total_value / good.quantity)

    # TotalCost — обязательная общая стоимость товарной позиции
    total = good.total_value or (
        (good.unit_price * good.quantity) if (good.unit_price and good.quantity) else Decimal('0')
    )
    el = etree.SubElement(g, f'{{{NS_CAT_COMFIN}}}TotalCost')
    el.text = decimal_to_str(total)


def _add_delivery_terms(
    parent: etree._Element,
    *,
    shipment_type: str,
    incoterms: str = DEFAULT_INCOTERMS,
) -> None:
    """InvoiceDeliveryTermsType. Обязательны:
    DeliveryTermsStringCode (Incoterms), DispatchCountry, TradingCountry, DestinationCountry.

    Коды стран для экспресс-грузов выводим из shipment_type:
    - IMPORT (импорт): dispatch=CN, trading=CN, destination=RU
    - EXPORT (экспорт): dispatch=RU, trading=RU, destination=CN
    """
    dt = etree.SubElement(parent, f'{{{NS_INV}}}DeliveryTerms')

    # Поля из ContractDeliveryTermsType (base)
    el = etree.SubElement(dt, f'{{{NS_CAT_COMFIN}}}DeliveryTermsStringCode')
    el.text = incoterms

    # Поля из InvoiceDeliveryTermsType (extension)
    is_import = (shipment_type == 'IMPORT')
    el = etree.SubElement(dt, f'{{{NS_INV}}}DispatchCountryCode')
    el.text = 'CN' if is_import else 'RU'
    el = etree.SubElement(dt, f'{{{NS_INV}}}TradingCountryCode')
    el.text = 'CN' if is_import else 'RU'
    el = etree.SubElement(dt, f'{{{NS_INV}}}DestinationCountryCode')
    el.text = 'RU' if is_import else 'CN'


def build(hawb) -> etree._Element:
    """Строит <Invoice> по одной HAWB.

    Возвращает etree._Element. Заворачивать в Envelope — слой выше.
    """
    root = etree.Element(
        f'{{{NS_INV}}}Invoice',
        attrib={'DocumentModeID': '1002007E'},
        nsmap=NSMAP_INV,
    )

    # 1. BaseDocType.DocumentID
    add_base_doc_fields(root)

    # 2. CurrencyCode (обязательно)
    el = etree.SubElement(root, f'{{{NS_INV}}}CurrencyCode')
    el.text = hawb.invoice_currency or 'USD'

    # 3. Места — опционально, но полезно
    if hawb.pieces_declared:
        el = etree.SubElement(root, f'{{{NS_INV}}}PlacesQuantity')
        el.text = str(hawb.pieces_declared)

    # 4. Веса — опц.
    goods = list(hawb.goods.all())
    gross_total = sum((g.weight_gross or Decimal(0) for g in goods), Decimal(0))
    if gross_total == 0 and hawb.weight:
        gross_total = hawb.weight
    if gross_total:
        el = etree.SubElement(root, f'{{{NS_INV}}}GrossWeightQuantity')
        el.text = decimal_to_str(gross_total, places=3)

    net_total = sum((g.weight_net or Decimal(0) for g in goods), Decimal(0))
    if net_total:
        el = etree.SubElement(root, f'{{{NS_INV}}}NetWeightQuantity')
        el.text = decimal_to_str(net_total, places=3)

    # 5. GCost — общая стоимость товаров (обязательно)
    g_cost = sum((g.total_value or Decimal(0) for g in goods), Decimal(0))
    if g_cost == 0:
        g_cost = hawb.invoice_value or Decimal(0)
    el = etree.SubElement(root, f'{{{NS_INV}}}GCost')
    el.text = decimal_to_str(g_cost)

    # 6. TotalCost — с учётом расходов/скидки (тут = GCost, без доп. расходов)
    el = etree.SubElement(root, f'{{{NS_INV}}}TotalCost')
    el.text = decimal_to_str(g_cost)

    # 7. Buyer (получатель/импортёр) — обязательно
    _add_participant(
        root, f'{{{NS_INV}}}Buyer',
        name=hawb.consignee_name,
        inn=hawb.consignee_inn,
    )

    # 8. Seler — продавец/экспортёр — обязательно (в схеме реально с одной "l")
    _add_participant(
        root, f'{{{NS_INV}}}Seler',
        name=hawb.shipper_name,
        inn=hawb.shipper_inn,
    )

    # 9. InvoiceGoods — товары (обязательно ≥ 1)
    if not goods:
        # Заглушка из шапки HAWB
        g = etree.SubElement(root, f'{{{NS_INV}}}InvoiceGoods')
        el = etree.SubElement(g, f'{{{NS_CAT_COMFIN}}}GoodsDescription')
        el.text = (hawb.description or 'Товары экспресс-отправки')[:250]
    else:
        for good in goods:
            _add_goods(root, good)

    # 10. DeliveryTerms — обязательно
    _add_delivery_terms(root, shipment_type=hawb.shipment_type)

    # 11. Registration — реквизиты инвойса как документа
    reg = etree.SubElement(root, f'{{{NS_INV}}}Registration')
    el = etree.SubElement(reg, f'{{{NS_CAT_RU}}}PrDocumentNumber')
    el.text = hawb.hawb_number
    el = etree.SubElement(reg, f'{{{NS_CAT_RU}}}PrDocumentDate')
    el.text = hawb.created_at.date().strftime('%Y-%m-%d')

    return root
