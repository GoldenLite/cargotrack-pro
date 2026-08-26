"""Разбор ExpressCargoDeclaration (ДТЭГ/ПТДЭГ) → структура для per-HAWB реестра.

Данные берём из НАШЕЙ БД: исходящая подача сохранена в
`AltaOutboxObservation.parsed_meta['raw_xml']` — CMN.11349/11023 (импорт),
CMN.11335/11024 (экспорт). find_submission_for_hawb() находит нужную obs по
номеру накладной; parse_express_cargo_declaration() разбирает XML в структуру;
select_house_shipment() достаёт блок ОДНОЙ накладной (для выборочной печати).

Парсер namespace-agnostic: матчит по ЛОКАЛЬНОМУ имени тега (в XML куча
namespace-префиксов cat_ru/RUScat_ru/RUDECLcat/... — привязываться к ним хрупко).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

# Типы исходящих сообщений, несущих подачу ДТЭГ с товарами (по покрытию 06.08).
SUBMISSION_MSG_TYPES = ['CMN.11349', 'CMN.11023', 'CMN.11335', 'CMN.11024']

# ТН ВЭД корреспонденции (печатная продукция без коммерческой ценности).
# По накладной с одним таким товаром отдельный ДТЭГ не требуется → из рассылки
# реестров такие исключаем (см. is_correspondence_hawb).
CORRESPONDENCE_TNVED = '4911990000'


def _lt(el) -> str:
    """Локальное имя тега без namespace."""
    return el.tag.rsplit('}', 1)[-1]


def _kids(el, name: str) -> list:
    return [c for c in el if _lt(c) == name] if el is not None else []


def _kid(el, name: str):
    if el is None:
        return None
    for c in el:
        if _lt(c) == name:
            return c
    return None


def _txt(el, name: str, default: str = '') -> str:
    c = _kid(el, name)
    return (c.text or '').strip() if (c is not None and c.text) else default


def _deep(el, name: str):
    for e in el.iter():
        if _lt(e) == name:
            return e
    return None


def _address(el) -> str:
    a = _kid(el, 'SubjectAddressDetails')
    if a is None:
        return ''
    parts = [_txt(a, k) for k in
             ('PostalCode', 'CounryName', 'Region', 'City', 'Town', 'StreetHouse')]
    return ', '.join(p for p in parts if p)


def _subject(el) -> dict:
    """ConsignorDetails / ConsigneeDetails → плоский dict."""
    if el is None:
        return {}
    feat = _kid(el, 'RFOrganizationFeatures')
    comm = _kid(el, 'CommunicationDetails')
    idc = _kid(el, 'IdentityCard')
    passport = ''
    if idc is not None:
        passport = ' '.join(x for x in (
            _txt(idc, 'IdentityCardName'), _txt(idc, 'IdentityCardSeries'),
            _txt(idc, 'IdentityCardNumber')) if x)
    return {
        'name': _txt(el, 'OrganizationName'),
        'inn': _txt(feat, 'INN') if feat is not None else '',
        'kpp': _txt(feat, 'KPP') if feat is not None else '',
        'phone': _txt(comm, 'Phone') if comm is not None else '',
        'passport': passport,
        'country': _txt(el, 'CountryA2Code'),
        'address': _address(el),
    }


def _goods_item(g) -> dict:
    descr = ' '.join((d.text or '').strip()
                     for d in _kids(g, 'GoodsDescription') if d.text)
    gw = _kid(g, 'GrossWeightQuantity')
    sq = _kid(g, 'SupplementaryQuantity')
    # CAValueAmount может быть 1-2: обычно [0]=стоимость по инвойсу (гр.12,
    # валюта отправителя), [-1]=таможенная стоимость (гр.13, RUB). Забираем всё.
    values = [{'value': _txt(v, 'CurrencyQuantity'), 'currency': _txt(v, 'CurrencyCode')}
              for v in _kids(g, 'CAValueAmount')]
    val = _kids(g, 'CAValueAmount')
    val = val[0] if val else None
    docs = [{
        'name': _txt(pd, 'PrDocumentName'),
        'number': _txt(pd, 'PrDocumentNumber'),
        'date': _txt(pd, 'PrDocumentDate'),
        'kind': _txt(pd, 'DocKindCode'),
    } for pd in _kids(g, 'PresentedDocDetails')]
    return {
        'num': _txt(g, 'GoodsNumeric'),
        'tnved': _txt(g, 'GoodsTNVEDCode'),
        'description': descr,
        'gross_kg': _txt(gw, 'GoodsQuantity') if gw is not None else '',
        'qty': _txt(sq, 'GoodsQuantity') if sq is not None else '',
        'qty_unit': _txt(sq, 'MeasureUnitQualifierName') if sq is not None else '',
        'qty_code': _txt(sq, 'MeasureUnitQualifierCode') if sq is not None else '',
        'value': _txt(val, 'CurrencyQuantity') if val is not None else '',
        'currency': _txt(val, 'CurrencyCode') if val is not None else '',
        'values': values,          # гр.12 инвойс + гр.13 там.стоимость
        'documents': docs,
    }


def _money(el, name: str) -> dict:
    """Узел с CurrencyQuantity/CurrencyCode → {value, currency}."""
    c = _kid(el, name)
    if c is None:
        return {'value': '', 'currency': ''}
    return {'value': _txt(c, 'CurrencyQuantity'), 'currency': _txt(c, 'CurrencyCode')}


def _quantity(el, name: str) -> dict:
    """Узел с GoodsQuantity/MeasureUnitQualifier* → {value, unit, code}."""
    c = _kid(el, name)
    if c is None:
        return {'value': '', 'unit': '', 'code': ''}
    return {
        'value': _txt(c, 'GoodsQuantity'),
        'unit': _txt(c, 'MeasureUnitQualifierName'),
        'code': _txt(c, 'MeasureUnitQualifierCode'),
    }


def _broker(ecd) -> dict:
    """BrokerName + BrokerRegistryDocDetails → декларант-ТП (шапка блока B)."""
    reg = _kid(ecd, 'BrokerRegistryDocDetails')
    return {
        'name': _txt(ecd, 'BrokerName'),
        'registry_doc_kind': _txt(reg, 'DocKindCode') if reg is not None else '',
        'registry_number': _txt(reg, 'RegistrationNumberId') if reg is not None else '',
    }


def _signatory(ecd) -> dict:
    """SignatoryPerson → подписант декларации (блок B: ФИО, паспорт, доверенность)."""
    sp = _kid(ecd, 'SignatoryPerson')
    if sp is None:
        return {}
    sd = _kid(sp, 'SigningDetails')
    comm = _kid(sd, 'CommunicationDetails') if sd is not None else None
    idc = _kid(sp, 'SignatoryPersonIdentityDetails')
    poa = _kid(sp, 'PowerOfAttorneyDetails')
    passport = {}
    if idc is not None:
        passport = {
            'name': _txt(idc, 'IdentityCardName'),
            'full_name': _txt(idc, 'FullIdentityCardName'),
            'series': _txt(idc, 'IdentityCardSeries'),
            'number': _txt(idc, 'IdentityCardNumber'),
            'date': _txt(idc, 'IdentityCardDate'),
            'issued_by': _txt(idc, 'OrganizationName'),
            'country': _txt(idc, 'CountryCode'),
        }
    power = {}
    if poa is not None:
        power = {
            'name': _txt(poa, 'PrDocumentName'),
            'number': _txt(poa, 'PrDocumentNumber'),
            'date': _txt(poa, 'PrDocumentDate'),
            'start': _txt(poa, 'DocStartDate'),
            'valid': _txt(poa, 'DocValidityDate'),
            'kind': _txt(poa, 'DocKindCode'),
        }
    return {
        'surname': _txt(sd, 'PersonSurname') if sd is not None else '',
        'name': _txt(sd, 'PersonName') if sd is not None else '',
        'middle': _txt(sd, 'PersonMiddleName') if sd is not None else '',
        'post': _txt(sd, 'PersonPost') if sd is not None else '',
        'phone': _txt(comm, 'Phone') if comm is not None else '',
        'signing_date': _txt(sd, 'SigningDate') if sd is not None else '',
        'passport': passport,
        'power_of_attorney': power,
    }


def _house_shipment(hs) -> dict:
    hwd = _kid(hs, 'HouseWaybillDetails')
    uw = _kid(hs, 'UnifiedGrossWeightQuantity')
    tvals = _kids(hs, 'CAValueAmount')
    tval = tvals[0] if tvals else None
    return {
        'waybill': _txt(hwd, 'PrDocumentNumber') if hwd is not None else '',
        'ordinal': _txt(hs, 'ObjectOrdinal'),
        'consignor': _subject(_kid(hs, 'ConsignorDetails')),
        'consignee': _subject(_kid(hs, 'ConsigneeDetails')),
        'goods': [_goods_item(g) for g in _kids(hs, 'GoodsItemDetails')],
        'total_gross_kg': _txt(uw, 'GoodsQuantity') if uw is not None else '',
        'total_value': _txt(tval, 'CurrencyQuantity') if tval is not None else '',
        'total_currency': _txt(tval, 'CurrencyCode') if tval is not None else '',
    }


def parse_express_cargo_declaration(xml_text) -> dict | None:
    """Разбирает XML подачи ДТЭГ в структуру. None если не ДТЭГ."""
    data = xml_text.encode('utf-8') if isinstance(xml_text, str) else xml_text
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    ecd = _deep(root, 'ExpressCargoDeclaration')
    if ecd is None:
        return None
    gs = _kid(ecd, 'GoodsShipment')
    if gs is None:
        return None
    loc = _kid(gs, 'GoodsLocationDetails')
    return {
        'registry_kind': _txt(ecd, 'ExpressRegistryKindCode'),   # ПТДЭГ
        'decl_kind': _txt(ecd, 'DeclarationKindCode'),           # ЭК / ИМ
        'customs_mode': _txt(ecd, 'CustomsModeCode'),            # 10 / 40
        'customs_office': _txt(loc, 'CustomsOfficeCode') if loc is not None else '',
        'customs_place': (_txt(loc, 'PlaceName') or _address(loc)) if loc is not None else '',
        'consignor': _subject(_kid(gs, 'ConsignorDetails')),     # общий
        'consignee': _subject(_kid(gs, 'ConsigneeDetails')),     # общий
        'broker': _broker(ecd),                                  # декларант-ТП (блок B)
        'signatory': _signatory(ecd),                            # подписант (блок B)
        # Итоги «Всего по ДТЭГ»:
        'total_gross': _quantity(gs, 'UnifiedGrossWeightQuantity'),
        'total_value': _money(gs, 'CAValueAmount'),
        'customs_cost': _money(gs, 'CustomsCost'),
        'total_payment': {                                       # блок D (импорт)
            'value': _txt(_kid(gs, 'TotalPaymentAmountDetails'), 'Amount'),
            'currency': _txt(_kid(gs, 'TotalPaymentAmountDetails'), 'CurrencyCode'),
        } if _kid(gs, 'TotalPaymentAmountDetails') is not None else {'value': '', 'currency': ''},
        'house_shipments': [_house_shipment(hs) for hs in _kids(gs, 'HouseShipment')],
    }


def select_house_shipment(parsed: dict, waybill: str) -> dict | None:
    for hs in (parsed or {}).get('house_shipments', []):
        if hs.get('waybill') == waybill:
            return hs
    return None


def _submission_obs_ids(hawb_number: str):
    """observation_id всех исходящих копий с этой накладной — через денорм
    AltaOutboxWaybill (индекс по hawb_number). Заменяет медленный
    parsed_meta__hawbs__contains (Parallel Seq Scan 559MB outbox)."""
    from cargo.models import AltaOutboxWaybill
    return list(AltaOutboxWaybill.objects
                .filter(hawb_number=hawb_number)
                .values_list('observation_id', flat=True))


def find_submission_for_hawb(hawb_number: str):
    """Ищет исходящую подачу ДТЭГ по номеру накладной в нашей БД.

    Возвращает (raw_xml, msg_type, observation) или (None, None, None).

    Быстрый путь — через денорм AltaOutboxWaybill (индекс). Медленный
    parsed_meta-скан — только fallback на промах денорма (пустой набор obs_ids).
    """
    from cargo.models import AltaOutboxObservation
    obs_ids = _submission_obs_ids(hawb_number)
    for mt in SUBMISSION_MSG_TYPES:
        if obs_ids:
            obs = (AltaOutboxObservation.objects
                   .filter(id__in=obs_ids, msg_type=mt)
                   .order_by('-received_at').first())
        else:
            obs = (AltaOutboxObservation.objects
                   .filter(msg_type=mt, parsed_meta__hawbs__contains=[hawb_number])
                   .order_by('-received_at').first())
        if not obs:
            continue
        rx = (obs.parsed_meta or {}).get('raw_xml') or ''
        if 'ExpressCargoDeclaration' in rx:
            return rx, mt, obs
    return None, None, None


def has_regular_dt_submission(hawb_number: str) -> bool:
    """True, если по накладной есть подача РЕГУЛЯРНОЙ ДТ (ESADout_CU), а не ДТЭГ.

    Регулярная ДТ (декларация на товары) — другой документ с другим печатным
    бланком; её обслуживает отдельная рассылка, а НЕ ДТЭГ-реестр. Отличаем по
    корню сохранённого raw_xml (ESADout_CU vs ExpressCargoDeclaration). CMN.11023
    несёт оба типа, поэтому смотрим содержимое, а не только msg_type.
    """
    from cargo.models import AltaOutboxObservation
    obs_ids = _submission_obs_ids(hawb_number)
    for mt in SUBMISSION_MSG_TYPES:
        if obs_ids:
            qs = AltaOutboxObservation.objects.filter(id__in=obs_ids, msg_type=mt)
        else:
            qs = AltaOutboxObservation.objects.filter(
                msg_type=mt, parsed_meta__hawbs__contains=[hawb_number])
        for obs in qs.order_by('-received_at')[:5]:
            rx = (obs.parsed_meta or {}).get('raw_xml') or ''
            if 'ESADout_CU' in rx:
                return True
    return False


def is_correspondence_hawb(hawb_number: str, rx=None) -> bool:
    """True, если по накладной РОВНО один товар с кодом ТН ВЭД 4911 99 000 0.

    Это корреспонденция (печатная продукция) — по каждой такой накладной
    отдельный ДТЭГ не требуется, поэтому из рассылки реестров их исключаем.
    rx можно передать заранее (уже найденный raw_xml подачи), чтобы не
    перезапрашивать БД. Любая неопределённость (нет подачи/накладной) → False,
    т.е. по умолчанию НЕ исключаем.
    """
    if rx is None:
        rx, _mt, _obs = find_submission_for_hawb(hawb_number)
    if not rx:
        return False
    parsed = parse_express_cargo_declaration(rx)
    if not parsed:
        return False
    hs = select_house_shipment(parsed, hawb_number)
    if hs is None:
        return False
    goods = hs.get('goods') or []
    if len(goods) != 1:
        return False
    tnved = re.sub(r'\D', '', goods[0].get('tnved') or '')
    return tnved == CORRESPONDENCE_TNVED


def reestr_data_for_hawb(hawb_number: str) -> dict | None:
    """Готовая структура реестра для ОДНОЙ накладной (для рендера).

    Соединяет общую шапку декларации + блок конкретной накладной.
    None если подача не найдена/накладной нет в ней.
    """
    rx, mt, obs = find_submission_for_hawb(hawb_number)
    if not rx:
        return None
    parsed = parse_express_cargo_declaration(rx)
    if not parsed:
        return None
    hs = select_house_shipment(parsed, hawb_number)
    if hs is None:
        return None
    return {
        'msg_type': mt,
        'declaration': {k: parsed[k] for k in (
            'registry_kind', 'decl_kind', 'customs_mode',
            'customs_office', 'customs_place', 'consignor', 'consignee',
            'broker', 'signatory', 'total_gross', 'total_value',
            'customs_cost', 'total_payment')},
        'house_shipment': hs,
        'total_hawbs': len(parsed.get('house_shipments', [])),
    }
