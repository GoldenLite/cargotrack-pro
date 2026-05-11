"""XSD-валидация XML-документов перед выгрузкой в hot-folder.

Используется кэш скомпилированных схем — XSD парсится один раз
за жизнь процесса. Бросает XSDValidationError со списком ошибок
или путём, по которому Альта-ГТД отвергнет документ.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from lxml import etree

from . import SCHEMAS_DIR


class XSDValidationError(Exception):
    """XML не прошёл XSD-валидацию."""

    def __init__(self, schema_name: str, errors: list[str]):
        self.schema_name = schema_name
        self.errors = errors
        details = '\n  '.join(errors[:10])
        more = f'\n  ... и ещё {len(errors) - 10}' if len(errors) > 10 else ''
        super().__init__(
            f'XML не соответствует {schema_name}:\n  {details}{more}'
        )


@lru_cache(maxsize=32)
def _load_schema(schema_name: str) -> etree.XMLSchema:
    schema_path = SCHEMAS_DIR / schema_name
    if not schema_path.exists():
        raise FileNotFoundError(f'XSD-схема не найдена: {schema_path}')
    # Парсер с base_url нужен, чтобы lxml сам резолвил <xs:import schemaLocation="...">
    parser = etree.XMLParser(load_dtd=False, no_network=True)
    schema_doc = etree.parse(str(schema_path), parser)
    return etree.XMLSchema(schema_doc)


def validate(xml_bytes: bytes, schema_name: str) -> None:
    """Валидирует XML против схемы. Бросает XSDValidationError, если не прошёл."""
    schema = _load_schema(schema_name)
    doc = etree.fromstring(xml_bytes)
    if not schema.validate(doc):
        errors = [
            f'строка {e.line}, столбец {e.column}: {e.message}'
            for e in schema.error_log
        ]
        raise XSDValidationError(schema_name, errors)
