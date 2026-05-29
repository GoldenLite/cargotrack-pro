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

    # CMN.13014 (WHGoodOut) — отчёт о выпуске груза со склада СВХ (ДО2).
    # Содержит рег.номер ДО2 (RegisterNumber), момент выпуска (SendDate+
    # SendTime), MAWB+HAWB партии (TransportDoc), лицензию СВХ и ссылку
    # на ДО-1 в свободном тексте Comments.
    if 'WHGoodOut' in xml_text or 'whgou:' in xml_text:
        base.update(parse_svh_do2_out(xml_text))

    # CMN.11350 (ExpressCargoDeclarationCustomMark) — отметки таможни.
    # Один XML содержит N блоков <Consignment>, каждый со своим DecisionCode
    # (10=выпуск, 70=запрос доков, 90=отказ) применённым к своему списку
    # IndividualWayBill. Решения per-HAWB → нельзя обобщать DecisionCode на
    # всё сообщение, нужен per-Consignment apply в inbox.dispatch.
    if 'ExpressCargoDeclarationCustomMark' in xml_text or 'ecdcm:' in xml_text:
        cons = parse_consignments(xml_text)
        if cons:
            base['consignments'] = cons

    return base


# ─── CMN.11350 (ExpressCargoDeclarationCustomMark) ──
#
# Структура (реальный пример 2026-05-20, 10 HAWB одной ДТ):
#
#   <ecdcm:ExpressCargoDeclarationCustomMark>
#     <cat_ru:DocumentID>46461302-…</cat_ru:DocumentID>
#     <ecdcm:ApplicationRegNumber>
#       <cat_ru:CustomsCode>10001020</cat_ru:CustomsCode>
#       <cat_ru:RegistrationDate>2026-05-19</cat_ru:RegistrationDate>
#       <cat_ru:GTDNumber>0018015</cat_ru:GTDNumber>
#     </ecdcm:ApplicationRegNumber>
#
#     <ecdcm:Consignment>                                ← один блок = одно решение
#       <ecdcm:DecisionCode>10</ecdcm:DecisionCode>      ← 10=выпуск, 90=отказ, 70=запрос
#       <ecdcm:DecisionDate>2026-05-19T11:26:23+03:00</…>
#       <ecdcm:IndividualWayBill>
#         <cat_ru:PrDocumentNumber>10262748701</…>       ← HAWB к которой относится решение
#       </ecdcm:IndividualWayBill>
#     </ecdcm:Consignment>
#     <ecdcm:Consignment>                                ← следующий HAWB, может другое решение
#       <ecdcm:DecisionCode>90</ecdcm:DecisionCode>      ← отказ для конкретно этой накладной
#       <ecdcm:DecisionDate>2026-05-20T20:40:49+03:00</…>
#       <ecdcm:IndividualWayBill>
#         <cat_ru:PrDocumentNumber>10260241143</…>
#       </ecdcm:IndividualWayBill>
#     </ecdcm:Consignment>
#     …
#
# Одна ДТ → может содержать смесь решений per-HAWB. Парсим список блоков,
# inbox.dispatch применяет решение каждого блока ТОЛЬКО к его HAWB.

_CONSIGNMENT_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?Consignment\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?Consignment>',
    re.S,
)

_PR_DOCUMENT_NUMBER_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?PrDocumentNumber\b[^>]*>([^<]+)</(?:[a-zA-Z][\w-]*:)?PrDocumentNumber>',
)


def parse_consignments(xml_text: str) -> list[dict]:
    """Список <ecdcm:Consignment> блоков из CMN.11350.

    Каждый элемент:
      decision_code  '10' | '70' | '90' | …  — решение таможни
      decision_date  ISO '2026-05-19T11:26:23+03:00' — когда вынесли
      reason_code    '409' и т.п. (если был отказ/запрос)
      reason_text    «Не представлены документы и сведения»
      waybills       ['10262748701', …] — все PrDocumentNumber внутри блока
                     (как правило одна HAWB, но схема допускает несколько)
    """
    out: list[dict] = []
    for m in _CONSIGNMENT_BLOCK_RE.finditer(xml_text):
        body = m.group(1)
        dc = _first(body, 'DecisionCode').strip()
        dd = _first(body, 'DecisionDate').strip()
        rc = _first(body, 'ReasonCode').strip()
        rt = _first(body, 'Reason').strip()
        waybills = [w.strip() for w in _PR_DOCUMENT_NUMBER_RE.findall(body)
                    if w.strip()]
        if dc or waybills:
            out.append({
                'decision_code': dc,
                'decision_date': dd,
                'reason_code':   rc,
                'reason_text':   rt,
                'waybills':      waybills,
            })
    return out


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
# DO1ReportLinkData — внутри DO1KeepLimits в том же CMN.13010. ReportNumber
# (вида "0000875") совпадает с report_number нашего исходящего ED.DO1
# (do1-<customs>-<date>-<NNNN>-<uuid8>). Точный якорь связки ДО1 → Cargo.
_DO1_REPORT_LINK_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?DO1ReportLinkData\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?DO1ReportLinkData>',
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

    # DO1ReportLinkData — связка с нашим ED.DO1 через report_number
    link = _DO1_REPORT_LINK_BLOCK_RE.search(xml_text)
    if link:
        body = link.group(1)
        out['do1_link_report_number'] = _first(body, 'ReportNumber')
        out['do1_link_report_date']   = _first(body, 'ReportDate')

    return out


# ─── CMN.13014 (WHGoodOut — ДО2) ──
#
# Структура (реальный пример 2026-05-24):
#
#   <whgou:WHGoodOut>
#     <cat_ru:DocumentID>dec1fd37-...</cat_ru:DocumentID>
#     <whgou:DocumentKind>GoodOutDecision</whgou:DocumentKind>
#     <whgou:RegisterNumber>                              ← рег.№ ДО2
#       <cat_ru:CustomsCode>10001020</cat_ru:CustomsCode>
#       <cat_ru:RegistrationDate>2026-05-24</cat_ru:RegistrationDate>
#       <cat_ru:GTDNumber>5049065</cat_ru:GTDNumber>
#     </whgou:RegisterNumber>
#     <whgou:SendDate>2026-05-24</whgou:SendDate>          ← дата выпуска
#     <whgou:SendTime>18:30:05.7096954</whgou:SendTime>    ← время выпуска
#     <whgou:Comments>1. ДО-1 Рег.№ 10001020/190326/5006404</whgou:Comments>  ← ссылка на ДО-1
#     <whgou:DeliveryGoods>
#       <whgou:GoodInfo>
#         <whgou:TransportDoc>
#           <cat_ru:PrDocumentNumber>170326-2</cat_ru:PrDocumentNumber>    ← MAWB
#         </whgou:TransportDoc>
#         <whgou:TransportDoc>
#           <cat_ru:PrDocumentNumber>10232984848</cat_ru:PrDocumentNumber> ← HAWB
#         </whgou:TransportDoc>
#       </whgou:GoodInfo>
#     </whgou:DeliveryGoods>
#     <whgou:SVHLicenceNumber>
#       <cat_ru:PrDocumentNumber>10001/060324/10009/1</cat_ru:PrDocumentNumber>  ← лицензия
#     </whgou:SVHLicenceNumber>
#   </whgou:WHGoodOut>

_TRANSPORT_DOC_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?TransportDoc\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?TransportDoc>',
    re.S,
)

_PRODUCE_DOCUMENTS_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?ProduceDocuments\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?ProduceDocuments>',
    re.S,
)

_SVH_LICENSE_NUMBER_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?SVHLicenceNumber\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?SVHLicenceNumber>',
    re.S,
)

_REGISTER_NUMBER_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?RegisterNumber\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?RegisterNumber>',
    re.S,
)

# Текст вида "1. ДО-1 Рег.№ 10001020/190326/5006404" в whgou:Comments
_DO1_REF_RE = re.compile(
    r'ДО[\s\-–—]*1\s+Рег\.?\s*№?\s*(\d+/\d+/\d+)',
    re.UNICODE,
)


def parse_svh_do2_out(xml_text: str) -> dict:
    """Парсит CMN.13014 (WHGoodOut, отчёт о выпуске со СВХ) → parsed_meta.

    Возвращаемые поля (префикс svh_do2_):
      svh_warehouse_license   — для фильтра «наш склад» в classify (как у CMN.13029/13010)
      svh_do2_reg_number      — собранный рег.номер ДО2 (10001020/240526/5049065)
      svh_do2_send_date       — 'YYYY-MM-DD'
      svh_do2_send_time       — 'HH:MM:SS.fff' (с микросекундами)
      svh_do2_release_date    — дата выпуска ДТ (информативно)
      svh_do2_doc_numbers     — список номеров из TransportDoc (MAWB, HAWB, транзитная)
      svh_do2_do1_ref         — рег.номер ДО-1 из Comments (для матчинга Cargo)
    """
    out: dict = {}

    # Лицензия СВХ — внутри SVHLicenceNumber блока (PrDocumentNumber туда же
    # попадает у TransportDoc, поэтому ищем точечно).
    lic_block = _SVH_LICENSE_NUMBER_BLOCK_RE.search(xml_text)
    if lic_block:
        out['svh_warehouse_license'] = _first(lic_block.group(1), 'PrDocumentNumber').strip()

    # SendDate + SendTime — момент выпуска со склада
    out['svh_do2_send_date'] = _first(xml_text, 'SendDate').strip()
    out['svh_do2_send_time'] = _first(xml_text, 'SendTime').strip()

    # ReleaseDate — дата выпуска ДТ (для информации)
    out['svh_do2_release_date'] = _first(xml_text, 'ReleaseDate').strip()

    # Рег.номер ДО2 = CustomsCode + RegistrationDate + GTDNumber внутри RegisterNumber
    reg_block = _REGISTER_NUMBER_BLOCK_RE.search(xml_text)
    if reg_block:
        rb = reg_block.group(1)
        cc = _first(rb, 'CustomsCode').strip()
        rd = _first(rb, 'RegistrationDate').strip()
        gn = _first(rb, 'GTDNumber').strip()
        if cc and rd and gn:
            try:
                y, m, d = rd.split('-')
                rd_short = f'{d}{m}{y[2:]}'
            except ValueError:
                rd_short = rd
            out['svh_do2_reg_number'] = f'{cc}/{rd_short}/{gn}'

    # Все номера документов из TransportDoc блоков (MAWB, HAWB, транзитная и т.п.)
    doc_numbers: list[str] = []
    for tdoc_m in _TRANSPORT_DOC_BLOCK_RE.finditer(xml_text):
        body = tdoc_m.group(1)
        num = _first(body, 'PrDocumentNumber').strip()
        if num:
            doc_numbers.append(num)
    out['svh_do2_doc_numbers'] = doc_numbers

    # Ссылка на ДО-1 из Comments — для альтернативного матчинга Cargo
    do1_ref_m = _DO1_REF_RE.search(xml_text)
    if do1_ref_m:
        out['svh_do2_do1_ref'] = do1_ref_m.group(1).strip()

    # Рег.номера ДТ из ProduceDocuments — ключ к per-HAWB матчингу.
    # ДО2 выпускает груз ПО конкретной ДТ → ищем HAWB у которых эта ДТ.
    # Структура:
    #   <whgou:ProduceDocuments>
    #     <cat_ru:PrDocumentName>ДТ</cat_ru:PrDocumentName>
    #     <cat_ru:PrDocumentNumber>5086913</cat_ru:PrDocumentNumber>  ← GTDNumber
    #     <cat_ru:PrDocumentDate>2026-03-24</cat_ru:PrDocumentDate>
    #     <catWH_ru:PresentedDocumentModeCode>09035</catWH_ru:PresentedDocumentModeCode>
    #     <whgou:CustomsCode>10131010</whgou:CustomsCode>
    #   </whgou:ProduceDocuments>
    # PresentedDocumentModeCode 09035 = «Декларация на товары».
    declarations: list[str] = []
    for pdoc_m in _PRODUCE_DOCUMENTS_BLOCK_RE.finditer(xml_text):
        body = pdoc_m.group(1)
        mode = _first(body, 'PresentedDocumentModeCode').strip()
        if mode != '09035':  # интересует только ДТ
            continue
        cc = _first(body, 'CustomsCode').strip()
        gn = _first(body, 'PrDocumentNumber').strip()
        pd = _first(body, 'PrDocumentDate').strip()
        if cc and gn and pd:
            try:
                y, m, d = pd.split('-')
                pd_short = f'{d}{m}{y[2:]}'
            except ValueError:
                pd_short = pd
            declarations.append(f'{cc}/{pd_short}/{gn}')
    out['svh_do2_declarations'] = declarations

    return out


# ── DO1Report (исходящее уведомление о приёме на СВХ) ──────────────────────
#
# Файлы лежат в C:\ALTA\SvhPro\ED2SVH\backup_out\do1-*.xml на рабочей
# виртуалке (резервная копия исходящих от ed2svh.exe). Содержимое — сырое
# тело документа без Envelope-обёртки.
#
# Структура:
#   <edcnt:ED_Container>
#     <edcnt:ContainerDoc><edcnt:DocBody>
#       <do1r:DO1Report>
#         <catWH_ru:ReportNumber>0000873</catWH_ru:ReportNumber>
#         <catWH_ru:ReportDate>2026-05-25</catWH_ru:ReportDate>
#         <catWH_ru:CertificateNumber>10001/060324/10009/1</catWH_ru:CertificateNumber>
#         ...
#         <do1r:TransportDocs>     ← MAWB-блок (02020)
#           <cat_ru:PrDocumentName>Авианакладная (AWB)</cat_ru:PrDocumentName>
#           <cat_ru:PrDocumentNumber>141-70382023</cat_ru:PrDocumentNumber>
#           <catWH_ru:PresentedDocumentModeCode>02020</catWH_ru:PresentedDocumentModeCode>
#         </do1r:TransportDocs>
#         <do1r:TransportDocs>     ← HAWB-блок (02021), повторяется
#           <cat_ru:PrDocumentNumber>10251976678</cat_ru:PrDocumentNumber>
#           <catWH_ru:PresentedDocumentModeCode>02021</catWH_ru:PresentedDocumentModeCode>
#         </do1r:TransportDocs>
#         ...
# MAWB в DO1Report лежит в отдельном блоке <do1r:MasterAirWayBill>
# (без PresentedDocumentModeCode):
#   <do1r:MasterAirWayBill>
#     <cat_ru:PrDocumentName>Авианакладная (AWB)</cat_ru:PrDocumentName>
#     <cat_ru:PrDocumentNumber>141-70382023</cat_ru:PrDocumentNumber>
#     <cat_ru:PrDocumentDate>2026-05-22</cat_ru:PrDocumentDate>
#   </do1r:MasterAirWayBill>
# А HAWB-ы — в <do1r:TransportDocs> с PresentedDocumentModeCode=02021.
_MASTER_AWB_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?MasterAirWayBill\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?MasterAirWayBill>',
    re.S
)
_TRANSPORT_DOCS_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?TransportDocs\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?TransportDocs>',
    re.S
)

# Goods-блоки в DO1Report — содержат вес/места per-HAWB:
#   <catWH_ru:Goods>
#     <catWH_ru:CargoPlace>
#       <catWH_ru:PlaceNumber>1</catWH_ru:PlaceNumber>      ← количество мест
#     </catWH_ru:CargoPlace>
#     <catWH_ru:BruttoVolQuant>
#       <catWH_ru:GoodsQuantity>0.062000</catWH_ru:GoodsQuantity>  ← вес кг
#     </catWH_ru:BruttoVolQuant>
#     <catWH_ru:GoodsWHNumber>10257142180</catWH_ru:GoodsWHNumber> ← HAWB
#   </catWH_ru:Goods>
_GOODS_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?Goods\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?Goods>',
    re.S
)


def parse_do1_report(xml_text: str) -> dict:
    """Парсит DO1Report.xml из backup_out: ReportNumber + MAWB + HAWB + per-HAWB Goods.

    Возвращает:
      {
        'report_number': '0000873',
        'report_date':   '2026-05-25',
        'certificate_number': '10001/060324/10009/1',
        'mawb':          '141-70382023',
        'hawbs':         ['10251976678', ...],
        'goods':         {  # per-HAWB вес + места (суммируются если >1 Goods на HAWB)
            '10257142180': {'weight': '0.062', 'places': 1},
            ...
        },
      }
    """
    out: dict = {
        'report_number': _first(xml_text, 'ReportNumber'),
        'report_date':   _first(xml_text, 'ReportDate'),
        'certificate_number': _first(xml_text, 'CertificateNumber'),
        'mawb':          '',
        'hawbs':         [],
        'goods':         {},
    }
    # MAWB — отдельный блок <MasterAirWayBill> без code
    m_awb = _MASTER_AWB_BLOCK_RE.search(xml_text)
    if m_awb:
        out['mawb'] = _first(m_awb.group(1), 'PrDocumentNumber').strip()
    # HAWB-ы — TransportDocs с PresentedDocumentModeCode=02021
    for m in _TRANSPORT_DOCS_BLOCK_RE.finditer(xml_text):
        body = m.group(1)
        mode = _first(body, 'PresentedDocumentModeCode').strip()
        num  = _first(body, 'PrDocumentNumber').strip()
        if num and mode == '02021':
            out['hawbs'].append(num)
    # Goods — per-HAWB вес и места. Если на одну HAWB несколько Goods —
    # суммируем (бывает при разных товарах в одной накладной).
    from decimal import Decimal, InvalidOperation
    for m in _GOODS_BLOCK_RE.finditer(xml_text):
        body = m.group(1)
        hawb = _first(body, 'GoodsWHNumber').strip()
        if not hawb:
            continue
        # Вес: GoodsQuantity внутри BruttoVolQuant
        weight_str = ''
        bvq = re.search(
            r'<(?:[a-zA-Z][\w-]*:)?BruttoVolQuant\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?BruttoVolQuant>',
            body, re.S
        )
        if bvq:
            weight_str = _first(bvq.group(1), 'GoodsQuantity').strip()
        # Места: PlaceNumber внутри CargoPlace
        places_str = ''
        cp = re.search(
            r'<(?:[a-zA-Z][\w-]*:)?CargoPlace\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?CargoPlace>',
            body, re.S
        )
        if cp:
            places_str = _first(cp.group(1), 'PlaceNumber').strip()

        entry = out['goods'].setdefault(hawb, {'weight': Decimal('0'), 'places': 0})
        if weight_str:
            try:
                entry['weight'] += Decimal(weight_str)
            except (InvalidOperation, ValueError):
                pass
        if places_str:
            try:
                entry['places'] += int(places_str)
            except (TypeError, ValueError):
                pass
    # Decimal → str для JSON-сериализации
    out['goods'] = {
        hn: {'weight': str(v['weight']), 'places': v['places']}
        for hn, v in out['goods'].items()
    }
    return out


# ─── Счётчик товарных позиций в CMN.11023 / CMN.11349 ──────────────────
#
# CMN.11023 (первичная подача): структура «один ДТ → N товаров (ESADout_CUGoods)
# → внутри каждого товара 0..N групп описания (GoodsGroupDescription)».
# Логика «позиции декларации»:
#   - если у товара ≥1 GoodsGroupDescription → каждая группа = 1 позиция;
#   - если нет → сам товар (через top-level GoodsDescription) = 1 позиция.
# Один счётчик присваивается ВСЕМ HAWB одной декларации.
#
# CMN.11349 (ECD-корректировка): структура «N HouseShipment → каждая = одна
# индивидуальная накладная (HAWB)». В каждом HouseShipment лежат
# GoodsDescription/GoodsGroupDescription. Считаем per-HAWB по той же логике.

_GOODS_ITEM_BLOCK_RE = re.compile(
    r'<(?:[\w-]+:)?ESADout_CUGoods\b[^>]*>(.*?)</(?:[\w-]+:)?ESADout_CUGoods>',
    re.S,
)
_GOODS_GROUP_OPEN_RE = re.compile(
    r'<(?:[\w-]+:)?GoodsGroupDescription\b',
)
_GOODS_DESC_OPEN_RE = re.compile(
    r'<(?:[\w-]+:)?GoodsDescription\b',
)
_HOUSE_SHIPMENT_BLOCK_RE = re.compile(
    r'<(?:[\w-]+:)?HouseShipment\b[^>]*>(.*?)</(?:[\w-]+:)?HouseShipment>',
    re.S,
)
_GOODS_ITEM_DETAILS_OPEN_RE = re.compile(
    r'<(?:[\w-]+:)?GoodsItemDetails\b',
)
# HAWB = PrDocumentNumber, у которого следующий «kind»-код = 02021
# (в CMN.11349 встречается DocKindCode, в outbound ED.DO1 — PresentedDocumentModeCode).
_HAWB_PAIR_RE = re.compile(
    r'<(?:[\w-]+:)?PrDocumentNumber\b[^>]*>([^<]+)</(?:[\w-]+:)?PrDocumentNumber>'
    r'[\s\S]{0,500}?'
    r'<(?:[\w-]+:)?(?:DocKindCode|PresentedDocumentModeCode)\b[^>]*>'
    r'([^<]+)'
    r'</(?:[\w-]+:)?(?:DocKindCode|PresentedDocumentModeCode)>'
)


def count_positions_cmn_11023(xml_text: str) -> int:
    """Общее число позиций в декларации CMN.11023.

    Логика: каждый <ESADout_CUGoods> — один «товар» (TotalGoodsNumber).
    Внутри:
      - если ≥1 <GoodsGroupDescription> → +N (по числу групп);
      - иначе если есть <GoodsDescription> → +1;
    Итого — сумма по всем товарам декларации.
    """
    total = 0
    for m in _GOODS_ITEM_BLOCK_RE.finditer(xml_text):
        body = m.group(1)
        n_groups = len(_GOODS_GROUP_OPEN_RE.findall(body))
        if n_groups > 0:
            total += n_groups
        elif _GOODS_DESC_OPEN_RE.search(body):
            total += 1
    return total


def count_positions_per_hawb_cmn_11349(xml_text: str) -> dict:
    """Per-HAWB словарь {hawb_number: количество позиций} для CMN.11349.

    Логика: внутри каждого <HouseShipment> считаем число <GoodsItemDetails>
    — каждый элемент = одна товарная позиция. GoodsDescription может быть
    разбит на несколько тегов (длинный текст переносится по строкам), и
    использовать его как счётчик нельзя.
    """
    out: dict = {}
    for m in _HOUSE_SHIPMENT_BLOCK_RE.finditer(xml_text):
        body = m.group(1)
        hawb = ''
        for nm, mode in _HAWB_PAIR_RE.findall(body):
            if mode.strip() == '02021':
                hawb = nm.strip()
                break
        if not hawb:
            continue
        n = len(_GOODS_ITEM_DETAILS_OPEN_RE.findall(body))
        out[hawb] = out.get(hawb, 0) + n
    return out


def hawb_for_position_cmn_11349(xml_text: str, position: int) -> str:
    """Возвращает HAWB-номер, к которому относится N-я товарная позиция декларации.

    Используется при матчинге MY.11003 (запросы таможни) к конкретной HAWB.
    В MY.11003 есть <rid:Position> — порядковый номер товара в декларации.
    HouseShipment'ы идут в порядке появления; в каждом N товаров
    (GoodsItemDetails). Position=K → определяем какой HouseShipment
    содержит K-ю позицию.

    Пример: HouseShipment1 (3 товара) → позиции 1-3; HouseShipment2
    (1 товар) → позиция 4; HouseShipment3 (1) → позиция 5;
    HouseShipment4 (5) → позиции 6-10.

    Возвращает HAWB или '' если позиция вне диапазона / HAWB не нашли.
    """
    if not position or position < 1:
        return ''
    cursor = 0
    for m in _HOUSE_SHIPMENT_BLOCK_RE.finditer(xml_text):
        body = m.group(1)
        hawb = ''
        for nm, mode in _HAWB_PAIR_RE.findall(body):
            if mode.strip() == '02021':
                hawb = nm.strip()
                break
        n = len(_GOODS_ITEM_DETAILS_OPEN_RE.findall(body))
        if n <= 0:
            continue
        if cursor < position <= cursor + n:
            return hawb
        cursor += n
    return ''


# ─── MY.11003 (запрос документов от таможни) ──────────────────────────
#
# Структура (разобрано 2026-05-28 на 12 дампах):
#   <env:Header>
#     <roi:EnvelopeID>UUID-этого-сообщения</roi:EnvelopeID>
#     <InitialEnvelopeID>UUID-нашей-исходящей-CMN.11349</InitialEnvelopeID>
#     <roi:PreparationDateTime>...</roi:PreparationDateTime>
#   </env:Header>
#   <env:Body>
#     <rid:ReqInventoryDoc>
#       <rid:RequestNumber>1</rid:RequestNumber>
#       <rid:RequestDate>2026-05-28</rid:RequestDate>
#       <rid:RequestTime>17:04:22+10:00</rid:RequestTime>   ← TZ таможни
#       <rid:DateLimit>2026-05-29</rid:DateLimit>
#       <rid:RequestedDoc>
#         <cat_ru:PrDocumentName>текст запроса</cat_ru:PrDocumentName>
#         <rid:Position>2</rid:Position>                    ← позиция товара
#         <rid:RequestorName>имя инспектора</rid:RequestorName>
#       </rid:RequestedDoc>
#       <rid:Customs>
#         <cat_ru:Code>10702020</cat_ru:Code>
#         <cat_ru:OfficeName>т/п Первомайский</cat_ru:OfficeName>
#       </rid:Customs>
#     </rid:ReqInventoryDoc>
#   </env:Body>
#
# Один MY.11003 = один запрос. Несколько запросов на одну подачу →
# несколько отдельных файлов с одинаковым InitialEnvelopeID.


_REQUESTED_DOC_BLOCK_RE = re.compile(
    r'<(?:[\w-]+:)?RequestedDoc\b[^>]*>(.*?)</(?:[\w-]+:)?RequestedDoc>',
    re.S,
)


def parse_ed_11003(xml_text: str) -> dict:
    """Парсит ED.11003 (запросы документов от таможни) → parsed_meta.

    Один envelope = массив <RequestedDoc>. Возвращает общую шапку +
    список requests (по одному на каждый <RequestedDoc>).

    Структура:
      {
        'envelope_id', 'initial_envelope_id', 'prepared_at',
        'send_date', 'request_date', 'request_time', 'date_limit',
        'customs_code', 'office_name',
        'requests': [
          {'request_position_id': uuid,
           'position': '1',
           'request_text': 'Сообщаем, что...',
           'doc_code': '09023',
           'requestor_name': 'Иванов И.И.',
           'req_purpose': 'В целях...',
           'note': 'В соответствии со ст.325 ТК',
           'date_limit': '2026-05-29',  # может быть на уровне запроса
           'request_dt_msk': 'YYYY-MM-DDTHH:MM:SS+03:00'},
          ...
        ]
      }
    """
    from datetime import datetime, timezone as _tz, timedelta

    out = {
        'envelope_id':         _first(xml_text, 'EnvelopeID'),
        'initial_envelope_id': _first(xml_text, 'InitialEnvelopeID'),
        'prepared_at':         _first(xml_text, 'PreparationDateTime'),
        'request_number':      _first(xml_text, 'RequestNumber'),
        'send_date':           _first(xml_text, 'SendDate'),
        'request_date':        _first(xml_text, 'RequestDate'),
        'request_time':        _first(xml_text, 'RequestTime'),
        'date_limit':          _first(xml_text, 'DateLimit'),
        'customs_code':        _first(xml_text, 'CustomsCode'),
        'office_name':         _first(xml_text, 'OfficeName'),
    }
    # request_dt_msk считаем один раз для всех запросов в этом envelope
    request_dt_msk = ''
    if out['request_date'] and out['request_time']:
        try:
            iso = f'{out["request_date"]}T{out["request_time"]}'
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            msk = dt.astimezone(_tz(timedelta(hours=3)))
            request_dt_msk = msk.isoformat()
        except (ValueError, TypeError):
            pass

    # Парсим каждый <RequestedDoc>
    requests = []
    for m in _REQUESTED_DOC_BLOCK_RE.finditer(xml_text):
        body = m.group(1)
        req = {
            'request_position_id': _first(body, 'RequestPositionID'),
            'position':            _first(body, 'Position'),
            'request_text':        _first(body, 'PrDocumentName'),
            'doc_code':            _first(body, 'DocCode'),
            'requestor_name':      _first(body, 'RequestorName'),
            'req_purpose':         _first(body, 'ReqPurpose'),
            'note':                _first(body, 'Note'),
            # date_limit может быть в верхней шапке или per-request
            'date_limit':          _first(body, 'DateLimit') or out['date_limit'],
            'request_dt_msk':      request_dt_msk,
        }
        if req['request_text'] or req['request_position_id']:
            requests.append(req)

    out['requests'] = requests
    return out


# Совместимость: parse_my_11003 — алиас (старый код может вызывать).
parse_my_11003 = parse_ed_11003


# ─── Экспортные сообщения (CMN.11335 / CMN.11349 ЭК / CMN.11024 ЭК) ───
#
# Маркеры экспорта:
#   CMN.11335 / CMN.11349 — ExpressCargoDeclaration → <DeclarationKindCode>ЭК</…>
#   CMN.11024             — ESADout_CU             → <CustomsProcedure>ЭК</…>
#
# HAWB-номер всегда лежит в PrDocumentNumber с kind=02021 (DocKindCode или
# PresentedDocumentModeCode — _HAWB_PAIR_RE уже умеет оба).
#
# Транспортный документ для экспорта (что юзер хочет видеть в Sheets как «номер
# транспортного документа»):
#   CMN.11335 / CMN.11349 — PrDocumentNumber c DocKindCode=02099
#     (HouseShipment > TransportDocumentDetails). Формат «CDEK-XX-NNNN».
#   CMN.11024 — TransportDocument > PrDocumentNumber c PresentedDocumentModeCode
#     =02099 (в шапке ESADout_CUGoodsShipment). Формат также «CDEK-XX-NNNN».

_DECL_KIND_CODE_RE = re.compile(
    r'<(?:[\w-]+:)?DeclarationKindCode\b[^>]*>([^<]+)</(?:[\w-]+:)?DeclarationKindCode>'
)
_CUSTOMS_PROCEDURE_RE = re.compile(
    r'<(?:[\w-]+:)?CustomsProcedure\b[^>]*>([^<]+)</(?:[\w-]+:)?CustomsProcedure>'
)
_SIGNATORY_BLOCK_RE = re.compile(
    r'<(?:[\w-]+:)?SignatoryPerson\b[^>]*>(.*?)</(?:[\w-]+:)?SignatoryPerson>',
    re.S,
)
# CMN.11024 (классическая ДТ) хранит подписанта в FilledPerson, а не
# SignatoryPerson. Структура SigningDetails внутри идентична.
_FILLED_PERSON_BLOCK_RE = re.compile(
    r'<(?:[\w-]+:)?FilledPerson\b[^>]*>(.*?)</(?:[\w-]+:)?FilledPerson>',
    re.S,
)


def extract_signatory_name(xml_text: str) -> str:
    """Собирает ФИО декларанта из SignatoryPerson или FilledPerson.

    Структура (CMN.11335/11349/11023): SignatoryPerson > SigningDetails >
    Person{Surname,Name,MiddleName}.
    Структура (CMN.11024): FilledPerson > SigningDetails > Person{...} —
    тот же набор тэгов, другой родительский блок.

    Возвращает 'Фамилия Имя Отчество' или '' если блока нет.
    """
    m = _SIGNATORY_BLOCK_RE.search(xml_text)
    if not m:
        m = _FILLED_PERSON_BLOCK_RE.search(xml_text)
    if not m:
        return ''
    body = m.group(1)
    surname = _first(body, 'PersonSurname')
    name = _first(body, 'PersonName')
    middle = _first(body, 'PersonMiddleName')
    parts = [p for p in [surname, name, middle] if p]
    return ' '.join(parts)
# Транспортный документ (DocKindCode=02099). Тот же паттерн что у HAWB-pair,
# только проверяем код 02099 а не 02021.
_TRANSPORT_PAIR_RE = _HAWB_PAIR_RE  # тот же regex — фильтруем по коду в _hawb_and_transport_in_houseshipment


def _hawb_and_transport_in_houseshipment(body: str) -> tuple[str, str]:
    """Из тела одного <HouseShipment> возвращает (hawb_number, transport_doc).

    Используется для CMN.11335/11349 — там per-HAWB HouseShipment с двумя
    PrDocumentNumber: один HAWB (02021), второй транспортный (02099).
    """
    hawb = ''
    transport = ''
    for nm, mode in _HAWB_PAIR_RE.findall(body):
        m = mode.strip()
        if m == '02021' and not hawb:
            hawb = nm.strip()
        elif m == '02099' and not transport:
            transport = nm.strip()
    return hawb, transport


def parse_cmn_11335(xml_text: str) -> dict:
    """Парсер CMN.11335 (предварительная ДТЭГ, ПТДЭГ).

    Структура: ExpressCargoDeclaration → DeclarationKindCode → HouseShipment
    (по одному на HAWB). Каждая HouseShipment содержит:
    - HouseWaybillDetails.PrDocumentNumber (DocKindCode=02021) → HAWB
    - TransportDocumentDetails.PrDocumentNumber (DocKindCode=02099) → транспорт. док (CDEK-XX-NNNN)
    - N GoodsItemDetails → количество позиций per-HAWB

    Возвращает:
      {
        'declaration_kind': 'ЭК'|'ИМ'|'',
        'hawbs': ['10269627133', ...],
        'transport_per_hawb': {'10269627133': 'CDEK-AZ-3045', ...},
        'goods_count_per_hawb': {'10269627133': 8, ...},
      }
    """
    out = {
        'declaration_kind':     '',
        'hawbs':                [],
        'transport_per_hawb':   {},
        'goods_count_per_hawb': {},
        'signatory':            extract_signatory_name(xml_text),
    }
    m = _DECL_KIND_CODE_RE.search(xml_text)
    if m:
        out['declaration_kind'] = m.group(1).strip()
    for hs in _HOUSE_SHIPMENT_BLOCK_RE.finditer(xml_text):
        body = hs.group(1)
        hawb, transport = _hawb_and_transport_in_houseshipment(body)
        if not hawb:
            continue
        out['hawbs'].append(hawb)
        if transport:
            out['transport_per_hawb'][hawb] = transport
        out['goods_count_per_hawb'][hawb] = len(
            _GOODS_ITEM_DETAILS_OPEN_RE.findall(body))
    return out


def parse_cmn_11349_meta(xml_text: str) -> dict:
    """Расширенный парсер CMN.11349 (ДТЭГ): то же что parse_cmn_11335 + DeclarationKindCode.

    Структурно CMN.11335 и CMN.11349 идентичны, разница только в семантике
    (предварительная vs итоговая). Используем одну функцию.
    """
    return parse_cmn_11335(xml_text)


def parse_cmn_11024(xml_text: str) -> dict:
    """Парсер CMN.11024 (классическая ДТ).

    Структура: ESADout_CU → CustomsProcedure → ESADout_CUGoodsShipment →
    ESADout_CUGoods (N штук = TotalGoodsNumber). HAWB лежит в каждом
    ESADout_CUGoods → ESADout_CUPresentedDocument с PresentedDocumentModeCode
    =02021 (одна и та же на все товары если декларация на одну партию).

    Возвращает:
      {
        'customs_procedure':  'ЭК'|'ИМ'|'',
        'hawbs':              ['10255988260'],   # уникальные
        'transport_per_hawb': {'10255988260': 'CDEK-...'},  # если в шапке есть
        'goods_count':        12,    # = число ESADout_CUGoods (или групп внутри)
      }
    """
    out = {
        'customs_procedure':  '',
        'hawbs':              [],
        'transport_per_hawb': {},
        'goods_count':        0,
        'signatory':          extract_signatory_name(xml_text),
    }
    m = _CUSTOMS_PROCEDURE_RE.search(xml_text)
    if m:
        out['customs_procedure'] = m.group(1).strip()

    # HAWB — все уникальные PrDocumentNumber с kind=02021 в теле документа.
    hawbs_seen: list[str] = []
    for nm, mode in _HAWB_PAIR_RE.findall(xml_text):
        if mode.strip() == '02021':
            v = nm.strip()
            if v and v not in hawbs_seen:
                hawbs_seen.append(v)
    out['hawbs'] = hawbs_seen

    # Транспортный документ (02099) — обычно один на всю декларацию.
    transport_doc = ''
    for nm, mode in _HAWB_PAIR_RE.findall(xml_text):
        if mode.strip() == '02099':
            transport_doc = nm.strip()
            break
    if transport_doc and hawbs_seen:
        for h in hawbs_seen:
            out['transport_per_hawb'][h] = transport_doc

    # Количество позиций — используем общий счётчик ESADout_CUGoods/GoodsGroup.
    out['goods_count'] = count_positions_cmn_11023(xml_text)
    return out
