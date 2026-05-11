"""Общие хелперы для всех генераторов ЭД ФТС.

Все ЭД-документы наследуются от cat_ru:BaseDocType, который требует
DocumentID (UUID) + DocumentModeID + опциональный DocumentNumber/DateTime.
Здесь же — namespace-карта 5.27.0 и базовые конверторы.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Optional

from lxml import etree

# Namespace'ы версии 5.27.0 для документов, наследующих BaseDocType
NS_CAT_RU = 'urn:customs.ru:CommonAggregateTypes:5.24.0'
NS_CLT_RU = 'urn:customs.ru:CommonLeafTypes:5.10.0'
NS_RUSCAT_RU = 'urn:customs.ru:RUSCommonAggregateTypes:5.24.0'
NS_CLT_TRANS_RU = 'urn:customs.ru:Information:TransportDocuments:TransportCommonLeafTypesCust:5.14.3'
NS_CAT_ESAD_CU = 'urn:customs.ru:CUESADCommonAggregateTypesCust:5.27.0'
NS_CLT_ESAD_CU = 'urn:customs.ru:CUESADCommonLeafTypes:5.17.0'


def base_doc_attrs(document_mode_id: str) -> dict:
    """Возвращает обязательные атрибуты BaseDocType."""
    return {'DocumentModeID': document_mode_id}


def add_base_doc_fields(
    parent: etree._Element,
    *,
    ref_document_id: Optional[str] = None,
    inn_sign: Optional[str] = None,
    mcd_id: Optional[str] = None,
) -> None:
    """Добавляет поля BaseDocType: DocumentID (обяз.) + 3 опц. поля.

    BaseDocType определён в CommonAggregateTypesCust.xsd и содержит ТОЛЬКО:
        DocumentID    — UUID документа (обязателен)
        RefDocumentID — UUID исходного документа (если этот — корректирующий)
        INNSign       — ИНН владельца МЧД
        MCD_ID        — рег. номер машиночитаемой доверенности

    Должна вызываться ПЕРВОЙ — до прочих xs:sequence-полей конкретного типа,
    т.к. порядок элементов в XSD значимый.
    """
    el = etree.SubElement(parent, f'{{{NS_CAT_RU}}}DocumentID')
    el.text = str(uuid.uuid4()).upper()

    if ref_document_id:
        el = etree.SubElement(parent, f'{{{NS_CAT_RU}}}RefDocumentID')
        el.text = ref_document_id

    if inn_sign:
        el = etree.SubElement(parent, f'{{{NS_CAT_RU}}}INNSign')
        el.text = inn_sign

    if mcd_id:
        el = etree.SubElement(parent, f'{{{NS_CAT_RU}}}MCD_ID')
        el.text = mcd_id


def decimal_to_str(value: Optional[Decimal], places: int = 2) -> str:
    """Decimal → строка с фиксированным числом знаков для XSD."""
    if value is None:
        return '0'
    quantized = Decimal(value).quantize(Decimal(10) ** -places)
    return format(quantized, 'f')


def empty_address(parent: etree._Element, tag: str) -> etree._Element:
    """Создаёт пустой <Address> с CountryCode='RU' — placeholder для
    случаев, когда реальный адрес ещё не заполнен. Все необязательные
    поля адреса опускаем, но CountryCode обязателен по AddressType.
    """
    addr = etree.SubElement(parent, tag)
    cc = etree.SubElement(addr, f'{{{NS_CAT_RU}}}CountryCode')
    cc.text = 'RU'
    return addr
