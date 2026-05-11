"""HouseWaybill (HAWB) -> Alta internal IndPost XML.

В отличие от других генераторов в этом пакете, IndPost выгружается не в
ФТС-схему, а во внутренний диалект Альты (корень <AltaIndPost>,
windows-1251, плоские UPPERCASE-поля без namespace). Эта форма попадает
в hot-folder Альты как родная — Каталог -> Загрузка из XML её принимает.

Workflow: эта почтовая накладная — источник, из которого Альта строит
реестр экспресс-грузов (ДТЭГ); индивидуальная накладная ЭД-2 формируется
автоматически при создании реестра.

Структура подсмотрена с реального экспорта (IndPost_10221678941.xml) +
C:\\ALTA\\data\\IndPost.dcf (внутренний field map Альты).
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional
from xml.sax.saxutils import escape

ALTA_VERSION = '2.0.262.13'
ALTA_ED_VER = '5_27_0'

MSK = timezone(timedelta(hours=3))


def _txt(value) -> str:
    if value is None:
        return ''
    return str(value).strip()


def _dec(value, places: int = 2) -> str:
    if value in (None, ''):
        return ''
    try:
        d = Decimal(str(value)).quantize(Decimal(10) ** -places)
    except Exception:
        return _txt(value)
    return format(d, 'f')


def _date(value) -> str:
    if value is None:
        return ''
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d')
    return _txt(value)


def _el(parent_lines: list, indent: int, tag: str, text) -> None:
    """Append <TAG>text</TAG> only if text is non-empty."""
    s = _txt(text)
    if not s:
        return
    parent_lines.append(f'{"  " * indent}<{tag}>{escape(s)}</{tag}>')


def _is_export(hawb) -> bool:
    return getattr(hawb, 'shipment_type', 'IMPORT') == 'EXPORT'


def build(
    hawb,
    *,
    customs_code: str = '',
    origin_country: str = 'CN',
    delivery_terms: str = 'DAP',
) -> bytes:
    """Собирает Alta IndPost XML (cp1251) из HouseWaybill.

    Аргументы:
        hawb: cargo.models.HouseWaybill (с подгруженным mawb и goods)
        customs_code: код таможни оформления (F0001), напр. '10005020'
        origin_country: код страны происхождения по умолчанию (для GOODS без указания)
        delivery_terms: базис поставки (DAP по умолчанию для экспресса)

    Возвращает: bytes в кодировке windows-1251, готовые к записи в hot-folder.
    """
    mawb = getattr(hawb, 'mawb', None)
    is_import = not _is_export(hawb)

    # ── Шапка атрибутов ──
    now_local = datetime.now(tz=MSK)
    time_attr = now_local.strftime('%Y-%m-%dT%H:%M:%S%z')
    time_attr = time_attr[:-2] + ':' + time_attr[-2:]  # 2026-05-12T00:04:24+03:00

    head = (
        f'<AltaIndPost time="{escape(time_attr, {chr(34): "&quot;"})}" '
        f'user="" Version="{ALTA_VERSION}" '
        f'FileName="" EDVer="{ALTA_ED_VER}" Comment="">'
    )

    lines: list[str] = [head]

    # ── Идентификация ──
    _el(lines, 1, 'NUM', hawb.hawb_number)
    if mawb is not None:
        _el(lines, 1, 'AVIANUM', mawb.awb_number)
        _el(lines, 1, 'AVIADATE', _date(mawb.departure_date or mawb.flight_date))

    # INVNUM/INVDATE — на HAWB нет invoice number, оставляем пустыми
    _el(lines, 1, 'TYPE', '0')

    # ── Получатель ──
    # CONSIGNEE_CHOICE: 2 = юр. лицо, 1 = физ. лицо.
    # Эвристика: если задан ИНН — юр. лицо.
    is_legal_consignee = bool(_txt(hawb.consignee_inn))
    _el(lines, 1, 'CONSIGNEE_CHOICE', '2' if is_legal_consignee else '1')
    if is_legal_consignee:
        _el(lines, 1, 'CONSIGNEE_SHORTNAME', hawb.consignee_name)
    else:
        # Для физ. лица в Альте есть PERSON_SURNAME/NAME/MIDDLE, но мы это не моделируем.
        # Имя кладём целиком в SHORTNAME — Альта позволит откорректировать при загрузке.
        _el(lines, 1, 'CONSIGNEE_SHORTNAME', hawb.consignee_name)

    if is_import:
        _el(lines, 1, 'CONSIGNEE_ADDRESS_COUNTRYCODE', 'RU')
        _el(lines, 1, 'CONSIGNEE_ADDRESS_COUNRYNAME', 'Россия')  # sic: Альта так и пишет
    _el(lines, 1, 'RFORGANIZATIONFEATURES_INN', hawb.consignee_inn)
    _el(lines, 1, 'CITY', hawb.consignee_city)
    _el(lines, 1, 'STREETHOUSE', hawb.consignee_address)

    # ── Отправитель ──
    _el(lines, 1, 'SENDER', hawb.shipper_name)
    is_legal_shipper = bool(_txt(hawb.shipper_inn))
    _el(lines, 1, 'CONSIGNOR_CHOICE', '2' if is_legal_shipper else '1')
    _el(lines, 1, 'CONSIGNOR_ADDRESS_CITY', hawb.shipper_city)
    _el(lines, 1, 'CONSIGNOR_ADDRESS_STREETHOUSE', hawb.shipper_address)

    # ── Параметры партии ──
    if mawb is not None:
        _el(lines, 1, 'ARRIVEDATE', _date(mawb.flight_date or mawb.departure_date))
    _el(lines, 1, 'ALLCOST', _dec(hawb.invoice_value, 2))
    _el(lines, 1, 'CURRENCY', hawb.invoice_currency)
    _el(lines, 1, 'ALLWEIGHT', _dec(hawb.weight, 3))
    _el(lines, 1, 'ORGCOUNTRY', origin_country)
    _el(lines, 1, 'F0001', customs_code)
    _el(lines, 1, 'DELIVERYTERMS_TRADINGCOUNTRYCODE', origin_country)
    _el(lines, 1, 'DELIVERYTERMS_DISPATCHCOUNTRYCODE', origin_country)
    _el(lines, 1, 'DELIVERYTERMS_DELIVERYTERMSSTRINGCODE', delivery_terms)

    # ── Товарные позиции ──
    goods = list(hawb.goods.all())
    if not goods:
        # Если позиций нет — генерим одну строку из описания HAWB, чтобы документ
        # не уехал в Альту пустым.
        lines.append('  <GOODS>')
        _el(lines, 2, 'DESCR', hawb.description or 'Не указано')
        _el(lines, 2, 'QTY', str(hawb.pieces_declared or 1))
        _el(lines, 2, 'COST', _dec(hawb.invoice_value, 2))
        _el(lines, 2, 'WEIGHT', _dec(hawb.weight, 3))
        lines.append('  </GOODS>')
    else:
        for g in goods:
            lines.append('  <GOODS>')
            descr = g.name
            extras = []
            if g.brand:
                extras.append(f'тм {g.brand}')
            if g.model:
                extras.append(f'мод. {g.model}')
            if g.article:
                extras.append(f'арт. {g.article}')
            if g.manufacturer:
                extras.append(f'изг. {g.manufacturer}')
            if extras:
                descr = f'{descr}; {", ".join(extras)}'
            _el(lines, 2, 'DESCR', descr)
            _el(lines, 2, 'QTY', _dec(g.quantity, 3))
            _el(lines, 2, 'COST', _dec(g.total_value or g.unit_price, 2))
            _el(lines, 2, 'WEIGHT', _dec(g.weight_net or g.weight_gross, 3))
            _el(lines, 2, 'TNVED', g.tnved_code)
            _el(lines, 2, 'REGTNVED', g.tnved_code)
            lines.append('  </GOODS>')

    lines.append('</AltaIndPost>')

    xml_text = '<?xml version="1.0" encoding="windows-1251"?>\n' + '\n'.join(lines) + '\n'
    return xml_text.encode('windows-1251', errors='replace')
