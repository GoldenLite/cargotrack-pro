"""Catalog of table-widget columns for DashboardWidget (cargo & hawb).

Column definition keys:
    key         — unique identifier used in widget.config.columns
    label       — header label (Russian)
    type        — cell renderer type used by frontend
    db_fields   — list of Django ORM field paths fetched via .values()
    sortable    — allowed as order_by (optional, default False)
    choices     — dict value→label (for 'choice'/'chip')
    css_map     — dict value→CSS class (for 'chip')
    decimals    — number of decimal places (for 'number')
    draft_flag  — name of a boolean field to render as "черновик" chip
    align       — 'left' | 'right' | 'center'
"""

from .models import STAGE_CHOICES, HouseWaybill, TRANSPORT_MODE_CHOICES


_STAGE_CSS = {
    'DRAFT':      'stage-draft',
    'FORMED':     'stage-formed',
    'DISPATCHED': 'stage-dispatched',
    'ARRIVED':    'stage-arrived',
    'CUSTOMS':    'stage-customs',
    'RELEASED':   'stage-released',
}

_HAWB_LOGISTICS_CSS = {
    'CREATED':         'stage-draft',
    'TO_ORIGIN_WH':    'stage-formed',
    'AT_ORIGIN_WH':    'stage-formed',
    'CONSOLIDATED':    'stage-formed',
    'READY_TO_SHIP':   'stage-dispatched',
    'EXPORT_CUSTOMS':  'stage-customs',
    'IN_TRANSIT_EXP':  'stage-dispatched',
    'ARRIVED_DEST':    'stage-arrived',
    'AT_SVH':          'stage-arrived',
    'IMPORT_CUSTOMS':  'stage-customs',
    'READY_DELIVERY':  'stage-released',
    'TO_SORT_CENTER':  'stage-released',
    'AT_SORT_CENTER':  'stage-released',
    'READY_TO_DEST':   'stage-released',
    'IN_TRANSIT_DEST': 'stage-released',
    'ARRIVED_FINAL':   'stage-released',
    'DELIVERED':       'stage-released',
    'RETURNED':        'stage-draft',
    'LOST':            'stage-draft',
}


CARGO_COLUMNS = [
    {'key': 'awb_number',   'label': 'AWB',              'type': 'link_mono',
     'db_fields': ['awb_number', 'is_draft'], 'draft_flag': 'is_draft', 'sortable': True},
    {'key': 'stage',        'label': 'Этап',             'type': 'chip',
     'db_fields': ['stage'], 'choices': dict(STAGE_CHOICES), 'css_map': _STAGE_CSS, 'sortable': True},
    {'key': 'is_draft',     'label': 'Черновик',         'type': 'bool',
     'db_fields': ['is_draft'], 'sortable': True},
    {'key': 'route',        'label': 'Маршрут',          'type': 'route',
     'db_fields': ['departure_iata', 'arrival_iata']},
    {'key': 'departure_iata','label': 'IATA вылета',     'type': 'text',
     'db_fields': ['departure_iata'], 'sortable': True},
    {'key': 'arrival_iata', 'label': 'IATA прилёта',     'type': 'text',
     'db_fields': ['arrival_iata'], 'sortable': True},
    {'key': 'flight_number','label': 'Рейс',             'type': 'flight',
     'db_fields': ['flight_number', 'flight_date'], 'sortable': True},
    {'key': 'flight_date',  'label': 'Дата прилёта',     'type': 'date',
     'db_fields': ['flight_date'], 'sortable': True},
    {'key': 'departure_date','label': 'Дата вылета',     'type': 'date',
     'db_fields': ['departure_date'], 'sortable': True},
    {'key': 'weight',       'label': 'Вес, кг',          'type': 'number',
     'db_fields': ['weight'], 'decimals': 1, 'sortable': True, 'align': 'right'},
    {'key': 'pieces',       'label': 'Мест',             'type': 'number',
     'db_fields': ['pieces_declared'], 'sortable': True, 'align': 'right'},
    {'key': 'warehouse',    'label': 'Склад (лицензия)', 'type': 'text',
     'db_fields': ['warehouse_license'], 'sortable': True},
    {'key': 'warehouse_name','label': 'Название склада', 'type': 'text',
     'db_fields': ['warehouse_name'], 'sortable': True},
    {'key': 'bond_location','label': 'Ячейка хранения',  'type': 'text',
     'db_fields': ['bond_location']},
    {'key': 'scan_into_bond','label': 'Въезд на склад',  'type': 'datetime',
     'db_fields': ['scan_into_bond'], 'sortable': True},
    {'key': 'scan_out_of_bond','label': 'Выезд со склада','type': 'datetime',
     'db_fields': ['scan_out_of_bond'], 'sortable': True},
    {'key': 'customs_declaration_number','label': 'Номер ДТ','type': 'mono',
     'db_fields': ['customs_declaration_number'], 'sortable': True},
    {'key': 'customs_status','label': 'Там. статус',     'type': 'text',
     'db_fields': ['customs_status'], 'sortable': True},
    {'key': 'release_date', 'label': 'Дата выпуска',     'type': 'datetime',
     'db_fields': ['release_date'], 'sortable': True},
    {'key': 'entry_date',   'label': 'Дата подачи ДТ',   'type': 'datetime',
     'db_fields': ['entry_date'], 'sortable': True},
    {'key': 'invoice_value','label': 'Стоимость',        'type': 'money',
     'db_fields': ['invoice_value', 'invoice_currency'], 'sortable': True, 'align': 'right'},
    {'key': 'customs_value_rub','label': 'Там. стоимость (RUB)','type': 'number',
     'db_fields': ['customs_value_rub'], 'decimals': 2, 'sortable': True, 'align': 'right'},
    {'key': 'duty_amount',  'label': 'Пошлина',          'type': 'number',
     'db_fields': ['duty_amount'], 'decimals': 2, 'sortable': True, 'align': 'right'},
    {'key': 'transportation_mode','label': 'Вид транспорта','type': 'choice',
     'db_fields': ['transportation_mode'], 'choices': {str(k): v for k, v in TRANSPORT_MODE_CHOICES},
     'sortable': True},
    {'key': 'shp_type',     'label': 'Тип отправителя',  'type': 'text',
     'db_fields': ['shp_type'], 'sortable': True},
    {'key': 'is_transit',   'label': 'Транзит',          'type': 'bool',
     'db_fields': ['is_transit'], 'sortable': True},
    {'key': 'is_self_clearance', 'label': 'ТО клиентом',  'type': 'bool',
     'db_fields': ['is_self_clearance'], 'sortable': True},
    {'key': 'description',  'label': 'Описание',         'type': 'text',
     'db_fields': ['description'], 'truncate': 40},
    {'key': 'description_ru','label': 'Описание (RU)',   'type': 'text',
     'db_fields': ['description_ru'], 'truncate': 40},
    {'key': 'created_at',   'label': 'Создан',           'type': 'datetime',
     'db_fields': ['created_at'], 'sortable': True},
    {'key': 'updated_at',   'label': 'Обновлён',         'type': 'datetime',
     'db_fields': ['updated_at'], 'sortable': True},
    {'key': 'sla_stage',    'label': 'SLA (этап)',       'type': 'sla_progress',
     'db_fields': ['stage', 'stage_changed_at'], 'align': 'center',
     'sla': {'entity_type': 'cargo', 'status_field': 'stage'}},
]

CARGO_DEFAULT_COLUMNS = [
    'awb_number', 'stage', 'route', 'weight', 'pieces', 'flight_number', 'warehouse',
]


HAWB_COLUMNS = [
    {'key': 'hawb_number',  'label': 'HAWB',             'type': 'link_mono',
     'db_fields': ['hawb_number'], 'sortable': True},
    {'key': 'logistics_status','label': 'Лог. статус',   'type': 'chip',
     'db_fields': ['logistics_status'], 'choices': dict(HouseWaybill.LOGISTICS_STATUS_CHOICES),
     'css_map': _HAWB_LOGISTICS_CSS, 'sortable': True},
    {'key': 'customs_status','label': 'Там. статус',     'type': 'choice',
     'db_fields': ['customs_status'], 'choices': dict(HouseWaybill.CUSTOMS_STATUS_CHOICES),
     'sortable': True},
    {'key': 'cargo_type',   'label': 'Тип груза',        'type': 'choice',
     'db_fields': ['cargo_type'], 'choices': dict(HouseWaybill.CARGO_TYPE_CHOICES), 'sortable': True},
    {'key': 'shipment_type','label': 'Направление',      'type': 'choice',
     'db_fields': ['shipment_type'], 'choices': dict(HouseWaybill.SHIPMENT_TYPE_CHOICES), 'sortable': True},
    {'key': 'weight',       'label': 'Вес, кг',          'type': 'number',
     'db_fields': ['weight'], 'decimals': 1, 'sortable': True, 'align': 'right'},
    {'key': 'pieces',       'label': 'Мест',             'type': 'number',
     'db_fields': ['pieces_declared'], 'sortable': True, 'align': 'right'},
    {'key': 'invoice_value','label': 'Стоимость',        'type': 'money',
     'db_fields': ['invoice_value', 'invoice_currency'], 'sortable': True, 'align': 'right'},
    {'key': 'mawb',         'label': 'MAWB',             'type': 'mono',
     'db_fields': ['mawb__awb_number'], 'sortable': True},
    {'key': 'consignee_name','label': 'Получатель',      'type': 'consignee',
     'db_fields': ['consignee_name', 'consignee_city'], 'sortable': True},
    {'key': 'consignee_city','label': 'Город получателя','type': 'text',
     'db_fields': ['consignee_city'], 'sortable': True},
    {'key': 'consignee_inn','label': 'ИНН получателя',   'type': 'text',
     'db_fields': ['consignee_inn']},
    {'key': 'consignee_phone','label': 'Телефон',        'type': 'text',
     'db_fields': ['consignee_phone']},
    {'key': 'consignee_email','label': 'Email',          'type': 'text',
     'db_fields': ['consignee_email']},
    {'key': 'customs_declaration_number','label': 'Номер ДТ','type': 'mono',
     'db_fields': ['customs_declaration_number']},
    {'key': 'release_date', 'label': 'Дата выпуска',     'type': 'datetime',
     'db_fields': ['release_date'], 'sortable': True},
    {'key': 'logistics_status_date','label': 'Дата лог. статуса','type': 'datetime',
     'db_fields': ['logistics_status_date']},
    {'key': 'customs_status_date','label': 'Дата там. статуса','type': 'datetime',
     'db_fields': ['customs_status_date']},
    {'key': 'scan_into_bond','label': 'Размещён на СВХ (ДО1)','type': 'datetime',
     'db_fields': ['scan_into_bond']},
    {'key': 'assigned_to',  'label': 'Ответственный',    'type': 'user',
     'db_fields': ['assigned_to__username', 'assigned_to__first_name', 'assigned_to__last_name'],
     'sortable': True},
    {'key': 'docs_ready',   'label': 'Документы',        'type': 'docs',
     'db_fields': ['doc_invoice', 'doc_packing_list', 'doc_permit', 'doc_tech_desc', 'docs_required']},
    {'key': 'description',  'label': 'Описание',         'type': 'text',
     'db_fields': ['description'], 'truncate': 40},
    {'key': 'notes',        'label': 'Примечания',       'type': 'text',
     'db_fields': ['notes'], 'truncate': 40},
    {'key': 'created_at',   'label': 'Создан',           'type': 'datetime',
     'db_fields': ['created_at'], 'sortable': True},
    {'key': 'updated_at',   'label': 'Обновлён',         'type': 'datetime',
     'db_fields': ['updated_at'], 'sortable': True},
    {'key': 'sla_logistics','label': 'SLA (логистика)',  'type': 'sla_progress',
     'db_fields': ['logistics_status', 'logistics_status_date'], 'align': 'center',
     'sla': {'entity_type': 'hawb', 'status_field': 'logistics_status'}},
    {'key': 'sla_customs',  'label': 'SLA (таможня)',    'type': 'sla_progress',
     'db_fields': ['customs_status', 'customs_status_date'], 'align': 'center',
     'sla': {'entity_type': 'hawb', 'status_field': 'customs_status'}},
]

HAWB_DEFAULT_COLUMNS = [
    'hawb_number', 'logistics_status', 'cargo_type', 'weight', 'pieces',
    'mawb', 'consignee_name', 'release_date',
]


def get_column_catalog(entity_type: str) -> list:
    return HAWB_COLUMNS if entity_type == 'hawb' else CARGO_COLUMNS


def get_default_columns(entity_type: str) -> list:
    return list(HAWB_DEFAULT_COLUMNS if entity_type == 'hawb' else CARGO_DEFAULT_COLUMNS)


def sortable_fields(entity_type: str) -> set:
    out = {'created_at'}
    for col in get_column_catalog(entity_type):
        if col.get('sortable'):
            out.add(col['db_fields'][0])
    return out


def serialize_column(col: dict) -> dict:
    """Strip the column dict to only fields needed by the frontend renderer."""
    out = {'key': col['key'], 'label': col['label'], 'type': col['type'],
           'db_fields': col['db_fields']}
    for k in ('choices', 'css_map', 'draft_flag', 'decimals', 'align', 'truncate', 'sortable'):
        if col.get(k) is not None:
            out[k] = col[k]
    return out
