"""Парсер XML входящих сообщений от Альты/таможни — на стороне Django.

Дублирует логику `alta_agent._parse_inbox_xml`, но с двумя отличиями:
1. Знает про КДТ (Корректировочные Декларации Таможенной стоимости) — XML с
   несколькими разными `<GTDNumber>` тегами. Приоритет:
   а. Внутри `<goom:GTDoutCustomsMark>` (это release stamp) → актуальная ДТ
   б. Иначе самая поздняя по `RegistrationDate` среди всех CustomsCode/Date/Number
   в. Иначе первое плоское `<GTDNumber>` (старая логика)
2. Используется в `reparse_alta_inbox` management-команде, которая пересобирает
   `parsed_meta` из уже сохранённого `raw_xml` без необходимости обновлять
   агент на рабочем сервере.

Когда агент тоже обновим — он начнёт сразу класть правильный gtd_number,
re-parse понадобится только для исторических сообщений.
"""
from __future__ import annotations

import re
from typing import Optional


def _tag_re(tag: str) -> re.Pattern:
    """<NS:Tag>contents</NS:Tag>, namespace опциональный."""
    return re.compile(
        r'<(?:[a-zA-Z][\w-]*:)?' + tag + r'\b[^>]*>([^<]*)</(?:[a-zA-Z][\w-]*:)?' + tag + r'>'
    )


def _first(xml_text: str, tag: str) -> str:
    m = _tag_re(tag).search(xml_text)
    return m.group(1).strip() if m else ''


# Тройка тегов CustomsCode/RegistrationDate/GTDNumber в каком-то блоке.
# Используется для поиска ВСЕХ упоминаний полной ДТ в документе.
_DECL_TRIPLE_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?CustomsCode\b[^>]*>([^<]+)</(?:[a-zA-Z][\w-]*:)?CustomsCode>\s*'
    r'<(?:[a-zA-Z][\w-]*:)?RegistrationDate\b[^>]*>([^<]+)</(?:[a-zA-Z][\w-]*:)?RegistrationDate>\s*'
    r'<(?:[a-zA-Z][\w-]*:)?GTDNumber\b[^>]*>([^<]+)</(?:[a-zA-Z][\w-]*:)?GTDNumber>',
    re.S
)


def _pick_effective_decl(xml_text: str) -> tuple[str, str, str]:
    """Возвращает (customs_code, registration_date, gtd_number) актуальной ДТ.

    Приоритет:
    1. Тройка внутри <goom:GTDoutCustomsMark> — release stamp.
    2. Тройка с самой поздней RegistrationDate (новейшая корректировочная).
    3. Любая первая тройка (обычный случай — там одна ДТ).
    """
    # 1. Release stamp
    mark_block = re.search(
        r'<(?:[a-zA-Z][\w-]*:)?GTDoutCustomsMark\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?GTDoutCustomsMark>',
        xml_text, re.S
    )
    if mark_block:
        m = _DECL_TRIPLE_RE.search(mark_block.group(1))
        if m:
            return (m.group(1).strip(), m.group(2).strip(), m.group(3).strip())

    # 2/3. Все тройки в документе → выбрать самую позднюю
    all_triples = _DECL_TRIPLE_RE.findall(xml_text)
    if not all_triples:
        # совсем нет тройки — fallback на отдельные теги
        return (_first(xml_text, 'CustomsCode'),
                _first(xml_text, 'RegistrationDate'),
                _first(xml_text, 'GTDNumber'))

    # Все тройки — выбираем максимум по дате. Дата формата 2026-04-09 сравнима как строка.
    best = max(all_triples, key=lambda t: t[1].strip())
    return (best[0].strip(), best[1].strip(), best[2].strip())


def parse_raw_xml(xml_text: str) -> dict:
    """Полный парсинг XML inbox-сообщения → dict для parsed_meta.

    Возвращает поля совместимые с теми, что кладёт агент в parsed_meta,
    плюс выбирает «правильную» ДТ при наличии нескольких упоминаний (КДТ).
    """
    cc, rd, gn = _pick_effective_decl(xml_text)
    base = {
        'envelope_id':        _first(xml_text, 'EnvelopeID'),
        'initial_envelope':   _first(xml_text, 'InitialEnvelopeID'),
        'msg_type':           _first(xml_text, 'MessageType'),
        'prepared_at':        _first(xml_text, 'PreparationDateTime'),
        'waybill_number':     _first(xml_text, 'WayBillNumber'),
        'declaration_number': _first(xml_text, 'DeclarationNumber'),
        'customs_code':       cc,
        'registration_date':  rd,
        'gtd_number':         gn,
        'decision_code':      _first(xml_text, 'DecisionCode'),
        'design_code':        _first(xml_text, 'Design'),
        'reason_code':        _first(xml_text, 'ReasonCode'),
        'reason_text':        _first(xml_text, 'Reason'),
        'resolution_text':    _first(xml_text, 'ResolutionDescription'),
        'ref_document_id':    _first(xml_text, 'RefDocumentID'),
        'result_code':        _first(xml_text, 'ResultCode'),
        'result_description': _first(xml_text, 'ResultDescription'),
    }

    # CMN.13029 (Опись СВХ / WHDocInventory) — отдельная семантика, тянет MAWB
    # и дату подачи ДО1. Парсится дополнительными полями, чтобы не загрязнять
    # основной парсер ED-таможни.
    if 'WHDocInventory' in xml_text or 'whdi:' in xml_text:
        base.update(parse_svh_inventory(xml_text))

    return base


# ─── СВХ (CMN.13029) ──

# Внутри <Receiver><SVH>…</SVH></Receiver> — лицензия + дата подачи ДО1
_SVH_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?SVH\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?SVH>',
    re.S
)

# Внутри <GoodsShipment>…</GoodsShipment> — параметры партии (MAWB и т.д.)
_GOODS_SHIPMENT_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?GoodsShipment\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?GoodsShipment>',
    re.S
)

# Внутри <RegNumberDoc>…</RegNumberDoc> — рег.номер представления
_REG_NUMBER_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?RegNumberDoc\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?RegNumberDoc>',
    re.S
)

# Внутри <Avia>…</Avia> — данные авиарейса
_AVIA_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?Avia\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?Avia>',
    re.S
)


def normalize_mawb(raw: str) -> str:
    """`222-.40333075` → `222-40333075`. Убирает точки и пробелы.

    Альта в XML вписывает MAWB с разделителями, наши Cargo.awb_number — без.
    """
    return (raw or '').replace('.', '').replace(' ', '').strip()


def parse_svh_inventory(xml_text: str) -> dict:
    """Парсит CMN.13029 (WHDocInventory) → словарь полей для parsed_meta.

    Извлекает MAWB, лицензию СВХ, дату подачи ДО1 и рег.номер представления.
    Опционально enrichment: перевозчик, рейс, вес, кол-во мест.

    Все поля префиксированы `svh_` чтобы не пересекаться с ED-парсером.
    """
    out: dict = {}

    # SVH-блок: лицензия + дата ДО1
    svh = _SVH_BLOCK_RE.search(xml_text)
    if svh:
        body = svh.group(1)
        out['svh_warehouse_license'] = _first(body, 'DocumentNumber')
        out['svh_do1_present_date']  = _first(body, 'DO1PresentDocumentDate')
        out['svh_do1_present_time']  = _first(body, 'DO1PresentDocumentTime')
        out['svh_doc_mode_code']     = _first(body, 'DocumentModeCode')

    # GoodsShipment: MAWB + параметры
    goods = _GOODS_SHIPMENT_BLOCK_RE.search(xml_text)
    if goods:
        body = goods.group(1)
        mawb_raw = _first(body, 'PrDocumentNumber')
        out['svh_mawb_raw'] = mawb_raw
        out['svh_mawb']     = normalize_mawb(mawb_raw)
        out['svh_pr_document_date'] = _first(body, 'PrDocumentDate')
        out['svh_pr_document_mode'] = _first(body, 'PresentedDocumentModeCode')
        out['svh_goods_description'] = _first(body, 'GoodsDescription')

    # RegNumberDoc: рег.номер представления (10001020/220526/5005840)
    reg = _REG_NUMBER_BLOCK_RE.search(xml_text)
    if reg:
        body = reg.group(1)
        cc = _first(body, 'CustomsCode')
        rd = _first(body, 'RegistrationDate')
        gn = _first(body, 'GTDNumber')
        if cc and rd and gn:
            try:
                y, m, d = rd.split('-')
                rd_short = f'{d}{m}{y[2:]}'
            except ValueError:
                rd_short = rd
            out['svh_presentation_reg_number'] = f'{cc}/{rd_short}/{gn}'
        out['svh_reg_customs_code']      = cc
        out['svh_reg_registration_date'] = rd
        out['svh_reg_gtd_number']        = gn

    # Опционально: дата описи (когда нет DO1PresentDocumentDate)
    iid = _first(xml_text, 'InventoryInstanceDate')
    if iid:
        out['svh_inventory_instance_date'] = iid

    # Авиарейс (enrichment)
    avia = _AVIA_BLOCK_RE.search(xml_text)
    if avia:
        body = avia.group(1)
        out['svh_flight_number'] = _first(body, 'FlightNumber')
        out['svh_flight_date']   = _first(body, 'FlightDate')

    return out
