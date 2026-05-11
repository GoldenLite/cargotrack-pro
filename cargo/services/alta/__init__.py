"""Интеграция с Альта-ГТД через генерацию XML-документов по схемам ФТС.

Архитектура:
    schemas/        — XSD-схемы ФТС (версия 5.27.0)
    envelope.py     — обёртка SOAP-Envelope с EDHeader/RoutingInf
    validator.py    — XSD-валидация перед выгрузкой
    hotfolder.py    — запись готового пакета в папку Альта-ГТД
    generators/     — генераторы по типам документов (по одному файлу на тип)

Все генераторы возвращают bytes (готовый XML). Подпись (Альта-Подпись)
и транспорт (СВД-Клиент) — отдельные слои на стороне Альта-ГТД.
"""
from __future__ import annotations

from pathlib import Path

SCHEMAS_DIR = Path(__file__).parent / 'schemas'
