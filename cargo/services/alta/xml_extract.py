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

    # CMN.13029 (Опись СВХ / WHDocInventory) — представление в таможню,
    # содержит MAWB и UUID документа (якорь для связи с CMN.13010).
    if 'WHDocInventory' in xml_text or 'whdi:' in xml_text:
        base.update(parse_svh_inventory(xml_text))

    # CMN.13010 (Регистрация ДО1 / DORegInfo) — реальная регистрация ДО1
    # на СВХ. Содержит дату и рег.номер ДО1, ссылается на представление
    # через RefDocumentID → DocumentID CMN.13029.
    if 'DORegInfo' in xml_text or 'dori:' in xml_text:
        base.update(parse_svh_do1_reg(xml_text))

    return base


# ─── СВХ (CMN.13029) ──
#
# Структура реального XML (разобрано 2026-05-23 на дампе msg #3979):
#
#   <whdi:WHDocInventory>
#     <whdi:InventoryInstanceDate>2026-05-23</whdi:InventoryInstanceDate>
#     <whdi:RegNumberDoc>
#       <cat_ru:CustomsCode>10001020</cat_ru:CustomsCode>
#       <cat_ru:RegistrationDate>2026-05-23</cat_ru:RegistrationDate>  ← дата размещения
#       <cat_ru:GTDNumber>5005877</cat_ru:GTDNumber>                   ← рег.номер описи
#     </whdi:RegNumberDoc>
#     <whdi:WarehouseOwner>
#       <catWH_ru:WarehouseLicense>
#         <catWH_ru:CertificateNumber>10001/060324/10009/1</catWH_ru:CertificateNumber>  ← лицензия
#         <catWH_ru:CertificateDate>2024-03-06</catWH_ru:CertificateDate>
#       </catWH_ru:WarehouseLicense>
#     </whdi:WarehouseOwner>
#     <whdi:GoodsShipment>
#       <cat_ru:PrDocumentNumber>220526-2</cat_ru:PrDocumentNumber>    ← MAWB/CMR
#       <catWH_ru:PresentedDocumentModeCode>02015</catWH_ru:PresentedDocumentModeCode>  ← 02015=CMR, 02017=авиа
#     </whdi:GoodsShipment>
#   </whdi:WHDocInventory>
#
# Время подачи ДО1 (`DO1PresentDocumentTime`) в реальных сообщениях отсутствует —
# scan_into_bond выставляется с временем 00:00.

# WarehouseLicense → CertificateNumber
_WAREHOUSE_LICENSE_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?WarehouseLicense\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?WarehouseLicense>',
    re.S
)

# GoodsShipment — параметры партии (MAWB, mode)
_GOODS_SHIPMENT_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?GoodsShipment\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?GoodsShipment>',
    re.S
)

# RegNumberDoc — рег.номер описи (= рег.номер размещения)
_REG_NUMBER_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?RegNumberDoc\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?RegNumberDoc>',
    re.S
)


def normalize_mawb(raw: str) -> str:
    """`222-.40333075` → `222-40333075`. Убирает точки и пробелы.

    Альта в авиа-XML вписывает MAWB с разделителем-точкой, наш Cargo
    хранит без точки. Для CMR-партий (формат `220526-2`) и других — no-op.
    """
    return (raw or '').replace('.', '').replace(' ', '').strip()


def parse_svh_inventory(xml_text: str) -> dict:
    """Парсит CMN.13029 (WHDocInventory, представление в таможню) → parsed_meta.

    Содержит: MAWB, лицензию СВХ, рег.номер ПРЕДСТАВЛЕНИЯ (не ДО1!) и
    `svh_document_id` — UUID документа, используется как якорь для связи
    с CMN.13010 (где он в RefDocumentID).

    Все поля префиксированы `svh_` чтобы не пересекаться с ED-парсером.
    """
    out: dict = {}

    # Лицензия СВХ
    lic_block = _WAREHOUSE_LICENSE_BLOCK_RE.search(xml_text)
    if lic_block:
        body = lic_block.group(1)
        out['svh_warehouse_license']  = _first(body, 'CertificateNumber')
        out['svh_warehouse_lic_date'] = _first(body, 'CertificateDate')
        out['svh_warehouse_lic_kind'] = _first(body, 'CertificateKind')

    # GoodsShipment: MAWB
    goods = _GOODS_SHIPMENT_BLOCK_RE.search(xml_text)
    if goods:
        body = goods.group(1)
        mawb_raw = _first(body, 'PrDocumentNumber')
        out['svh_mawb_raw'] = mawb_raw
        out['svh_mawb']     = normalize_mawb(mawb_raw)
        out['svh_pr_document_date'] = _first(body, 'PrDocumentDate')
        out['svh_pr_document_mode'] = _first(body, 'PresentedDocumentModeCode')

    # RegNumberDoc: рег.номер ПРЕДСТАВЛЕНИЯ (не ДО1!)
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
        out['svh_presentation_date'] = rd

    iid = _first(xml_text, 'InventoryInstanceDate')
    if iid:
        out['svh_inventory_instance_date'] = iid

    # Якорь связи: DocumentID представления = RefDocumentID в CMN.13010
    doc_id = _first(xml_text, 'DocumentID')
    if doc_id:
        out['svh_document_id'] = doc_id

    return out


# ─── CMN.13010 (Регистрация ДО1) ──
#
# Структура (разобрано 2026-05-23 на дампе msg #4240):
#
#   <dori:DORegInfo>
#     <cat_ru:DocumentID>3275d8d9-…</cat_ru:DocumentID>
#     <cat_ru:RefDocumentID>15e627c1-…</cat_ru:RefDocumentID>      ← = CMN.13029.DocumentID
#     <dori:RegDate>2026-05-23</dori:RegDate>                       ← ДАТА РЕГИСТРАЦИИ ДО1
#     <dori:RegTime>13:47:40.0991297</dori:RegTime>
#     <dori:FormReport>1</dori:FormReport>                           ← 1=ДО1, 2=ДО2
#     <dori:RegisterNumberReport>
#       <cat_ru:CustomsCode>10001020</cat_ru:CustomsCode>
#       <cat_ru:RegistrationDate>2026-05-23</cat_ru:RegistrationDate>
#       <cat_ru:GTDNumber>5012272</cat_ru:GTDNumber>                 ← РЕГ.НОМЕР ДО1
#     </dori:RegisterNumberReport>
#   </dori:DORegInfo>
#   <kl:DO1KeepLimits>
#     <kl:WarehouseOwner>
#       <catWH_ru:WarehouseLicense>
#         <catWH_ru:CertificateNumber>10001/060324/10009/1</…>       ← лицензия (фильтр!)
#       </catWH_ru:WarehouseLicense>
#     </kl:WarehouseOwner>
#     <!-- DO1GoodKeepingLimit × N — лимиты хранения товаров, нам не нужно -->
#   </kl:DO1KeepLimits>

_DOREG_INFO_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?DORegInfo\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?DORegInfo>',
    re.S
)
_REGISTER_NUMBER_REPORT_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?RegisterNumberReport\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?RegisterNumberReport>',
    re.S
)


def parse_svh_do1_reg(xml_text: str) -> dict:
    """Парсит CMN.13010 (DORegInfo, регистрация ДО1) → parsed_meta.

    Извлекает дату и рег.номер ДО1, форму отчёта (1=ДО1, 2=ДО2),
    ссылку на представление (`svh_ref_document_id` = CMN.13029.DocumentID).

    Лицензия — из вложенного блока WarehouseLicense (уже выкусит
    parse_svh_inventory если он сработал; но здесь WarehouseLicense
    лежит в DO1KeepLimits, не WHDocInventory — отдельно вызываем).
    """
    out: dict = {}

    # Лицензия СВХ (внутри DO1KeepLimits/WarehouseOwner/WarehouseLicense)
    lic_block = _WAREHOUSE_LICENSE_BLOCK_RE.search(xml_text)
    if lic_block:
        body = lic_block.group(1)
        out['svh_warehouse_license']  = _first(body, 'CertificateNumber')
        out['svh_warehouse_lic_date'] = _first(body, 'CertificateDate')
        out['svh_warehouse_lic_kind'] = _first(body, 'CertificateKind')

    # DORegInfo: основные данные регистрации ДО1
    doreg = _DOREG_INFO_BLOCK_RE.search(xml_text)
    if doreg:
        body = doreg.group(1)
        out['svh_do1_reg_date']    = _first(body, 'RegDate')
        out['svh_do1_reg_time']    = _first(body, 'RegTime')
        out['svh_do1_form_report'] = _first(body, 'FormReport')
        out['svh_ref_document_id'] = _first(body, 'RefDocumentID')

        # RegisterNumberReport: рег.номер ДО1
        rnr = _REGISTER_NUMBER_REPORT_RE.search(body)
        if rnr:
            rb = rnr.group(1)
            cc = _first(rb, 'CustomsCode')
            rd = _first(rb, 'RegistrationDate')
            gn = _first(rb, 'GTDNumber')
            if cc and rd and gn:
                try:
                    y, m, d = rd.split('-')
                    rd_short = f'{d}{m}{y[2:]}'
                except ValueError:
                    rd_short = rd
                out['svh_do1_reg_number'] = f'{cc}/{rd_short}/{gn}'

    return out
