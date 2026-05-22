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
    return {
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
