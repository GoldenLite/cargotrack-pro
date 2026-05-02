"""Реестр сущностей для dashboard-виджетов.

Единый источник правды о том, какие сущности (модели) доступны в виджетах,
какие у них поля для группировки/агрегации, и куда вести drill-down.

Источник группируемых/агрегируемых полей для 'cargo' и 'hawb' — ``cql_parser``
(чтобы не дублировать определения). Для новых сущностей (warehouse и т.д.) поля
определены прямо здесь.
"""

from .models import Cargo, HouseWaybill, Warehouse
from .cql_parser import GROUPABLE_FIELDS as _CQL_GROUPABLE
from .cql_parser import AGGREGATABLE_FIELDS as _CQL_AGGREGATABLE
from .widget_columns import CARGO_COLUMNS, HAWB_COLUMNS


WAREHOUSE_GROUPABLE = {
    'city':       {'orm': 'city',          'label': 'Город'},
    'iata_code':  {'orm': 'iata_code',     'label': 'IATA код'},
    'is_active':  {'orm': 'is_active',     'label': 'Активен',
                   'choices': {True: 'Да', False: 'Нет'}},
}

WAREHOUSE_AGGREGATABLE = {
    'max_capacity_kg': {'orm': 'max_capacity_kg', 'label': 'Макс. ёмкость, кг', 'type': 'num'},
}


ENTITY_REGISTRY = {
    'cargo': {
        'model':        Cargo,
        'label':        'Партии (MAWB)',
        'list_url':     'cargo_list',
        'groupable':    _CQL_GROUPABLE['cargo'],
        'aggregatable': _CQL_AGGREGATABLE['cargo'],
        'columns':      CARGO_COLUMNS,
    },
    'hawb': {
        'model':        HouseWaybill,
        'label':        'Накладные (HAWB)',
        'list_url':     'all_hawbs',
        'groupable':    _CQL_GROUPABLE['hawb'],
        'aggregatable': _CQL_AGGREGATABLE['hawb'],
        'columns':      HAWB_COLUMNS,
    },
    'warehouse': {
        'model':        Warehouse,
        'label':        'СВХ',
        'list_url':     None,
        'groupable':    WAREHOUSE_GROUPABLE,
        'aggregatable': WAREHOUSE_AGGREGATABLE,
        'columns':      [],
    },
}


def get_entities() -> list:
    """Список сущностей для field-first UI и API-ручки /widgets/entities/."""
    return [
        {'key': key, 'label': spec['label'], 'list_url': spec['list_url']}
        for key, spec in ENTITY_REGISTRY.items()
    ]


def get_entity_spec(entity_key: str) -> dict | None:
    return ENTITY_REGISTRY.get(entity_key)


def get_field_catalog_union() -> list:
    """Плоский каталог всех полей (groupable + aggregatable) из всех сущностей.

    Возвращает список словарей, каждый описывает одно вхождение поля.
    Если одинаковый ключ встречается в нескольких сущностях, формируется
    несколько записей — вызывающая сторона сама решает, как показать
    disambiguation.
    """
    catalog: list[dict] = []
    for entity_key, spec in ENTITY_REGISTRY.items():
        for field_key, field_def in spec['groupable'].items():
            catalog.append({
                'key':    field_key,
                'label':  field_def.get('label', field_key),
                'entity': entity_key,
                'role':   'groupable',
            })
        for field_key, field_def in spec['aggregatable'].items():
            catalog.append({
                'key':    field_key,
                'label':  field_def.get('label', field_key),
                'entity': entity_key,
                'role':   'aggregatable',
                'type':   field_def.get('type', 'num'),
            })
    return catalog


def resolve_entity_for_field(field_key: str, role: str = 'groupable') -> list:
    """Найти все сущности, в которых есть поле с заданным key и role.

    Возвращает список ``[{'entity': key, 'label': <entity_label>}, ...]``.
    Используется для disambiguation, если пользователь выбрал поле из
    field-first UI, а оно встречается в нескольких сущностях.
    """
    bucket_key = 'groupable' if role == 'groupable' else 'aggregatable'
    matches = []
    for entity_key, spec in ENTITY_REGISTRY.items():
        if field_key in spec[bucket_key]:
            matches.append({'entity': entity_key, 'label': spec['label']})
    return matches
