"""Угадываем вид транспорта по формату номера партии.

Используется при auto-создании Cargo из ImportedSheetRow.ТСД когда
номер партии не из стандартного AWB-формата. Пользователь может потом
поправить вручную в админке/UI.
"""
from __future__ import annotations

import re

# 4=Авиа, 3=Автомобильный, 1=Морской (см. cargo.models.TRANSPORT_MODE_CHOICES)
AVIA = 4
AUTO = 3
SEA  = 1

# AWB: 3 цифры (IATA-префикс перевозчика) + дефис + 8 цифр
_AWB_RE = re.compile(r'^\d{3}-\d{8}$')

# CMR: ДДММГГ-N (например 050526-2) или схожий формат с датой
_CMR_RE = re.compile(r'^\d{6}-\d+$')


def guess_transport_mode(number: str) -> int:
    """Возвращает transport_mode (1/3/4) по формату номера. Default — авиа."""
    s = (number or '').strip()
    if _AWB_RE.match(s):
        return AVIA
    if _CMR_RE.match(s):
        return AUTO
    # Коносамент / прочее — на глаз не отличить от CMR без префикса.
    # Оставляем default AVIA, пользователь поправит вручную.
    return AVIA
