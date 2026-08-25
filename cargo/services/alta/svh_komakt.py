"""Авто-комакт (коммерческий акт) для СВХ Внуково.

Собирает per-HAWB Excel-разрез товаров поданной ДТЭГ/ДТ, АГРЕГИРОВАННЫЙ по коду
ТН ВЭД (наименования через запятую, вес/стоимость суммируются), в формате,
который принимает СВХ. Заменяет ручную работу декларанта.

Данные берём из нашей БД (outbox подачи), парсер ДТЭГ переиспользуем из
dteg_reestr (parse_express_cargo_declaration/select_house_shipment) — там уже
есть per-позиция tnved/description/gross_kg/value/currency.

Только для нашего СВХ Внуково (лицензия начинается с 10001), ИМПОРТ.
См. [[svh-komakt-automation]].
"""
from __future__ import annotations

import io
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

VNUKOVO_LICENSE_PREFIX = '10001'

# Формат комакта = формат ДО1 (столбцы совпадают с ручным файлом).
HEADERS = [
    '№№',
    'HAWB, индивидуальная накладная',
    'Природа (характер) груза',
    'Количество мест, шт',
    'Вес брутто, кг',
    'Код ТН ВЭД',
    'Стоимость товара в валюте, указанной в транспортных или коммерческих документах',
    'Буквенный код валюты',
    'Получатель по HAWB, индивидуальной накладной',
]
SUBNUM = ['1', '2', '3', '5', '6', '7', '8', '9', '10']


def _dec(s) -> Decimal:
    if s is None:
        return Decimal('0')
    try:
        return Decimal(str(s).replace(',', '.').strip() or '0')
    except (InvalidOperation, ValueError):
        return Decimal('0')


def is_vnukovo_import(hawb) -> bool:
    """HAWB оформляется на нашем СВХ Внуково и это импорт."""
    c = hawb.mawb if getattr(hawb, 'mawb_id', None) else None
    if c is None:
        return False
    lic = (c.warehouse_license or '').strip()
    if not lic.startswith(VNUKOVO_LICENSE_PREFIX):
        return False
    return (hawb.shipment_type or 'IMPORT').upper() != 'EXPORT'


def extract_dt_positions(hawb_number: str):
    """(consignee_name, positions|None, reason). positions: list of
    {tnved, description, gross_kg, value, currency}. None + reason если не собрать.

    ДТЭГ (ExpressCargoDeclaration) — через штатный парсер. Регулярная ДТ
    (ESADout_CU) — пока не поддержана (нужен образец для проверки полей)."""
    from cargo.services.alta.dteg_reestr import (
        find_submission_for_hawb, parse_express_cargo_declaration,
        select_house_shipment, has_regular_dt_submission)

    rx, mt, _obs = find_submission_for_hawb(hawb_number)
    if not rx:
        if has_regular_dt_submission(hawb_number):
            return '', None, 'регулярная ДТ (ESADout_CU) — парсер комакта пока только ДТЭГ'
        return '', None, 'подача ДТЭГ не найдена в БД'
    parsed = parse_express_cargo_declaration(rx)
    if not parsed:
        return '', None, 'raw_xml не распознан как ДТЭГ'
    hs = select_house_shipment(parsed, hawb_number)
    if hs is None:
        return '', None, 'HouseShipment по накладной не найден в подаче'
    consignee = (hs.get('consignee') or {}).get('name') or ''
    positions = []
    for g in (hs.get('goods') or []):
        positions.append({
            'tnved': (g.get('tnved') or '').strip(),
            'description': (g.get('description') or '').strip(),
            'gross_kg': _dec(g.get('gross_kg')),
            'value': _dec(g.get('value')),
            'currency': (g.get('currency') or '').strip(),
        })
    if not positions:
        return consignee, None, 'в подаче нет товарных позиций'
    return consignee, positions, ''


def aggregate_by_tnved(positions):
    """GROUP BY код ТН ВЭД: наименования через запятую, Σ вес, Σ стоимость.
    Порядок групп = первого появления кода."""
    groups: 'OrderedDict[str, dict]' = OrderedDict()
    for p in positions:
        k = p['tnved']
        g = groups.get(k)
        if g is None:
            g = groups[k] = {'tnved': k, 'descs': [], 'gross_kg': Decimal('0'),
                             'value': Decimal('0'), 'currency': p['currency'], 'n': 0}
        if p['description']:
            g['descs'].append(p['description'])
        g['gross_kg'] += p['gross_kg']
        g['value'] += p['value']
        g['n'] += 1
        if p['currency']:
            g['currency'] = p['currency']
    return list(groups.values())


def _fnum(d: Decimal) -> float:
    return float(d)


def build_komakt_xlsx(hawb_number: str, place_count=None):
    """→ (xlsx_bytes, meta) | (None, {'reason': ...}).

    meta: {consignee, num_positions, num_codes, total_gross_kg, total_value,
    currency, place_count}."""
    from cargo.models import HouseWaybill
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, Border, Side

    h = HouseWaybill.objects.filter(hawb_number=hawb_number).order_by('-id').first()
    if not h:
        return None, {'reason': 'HAWB не найдена в БД'}
    if place_count is None:
        place_count = h.svh_do1_place_count

    consignee, positions, reason = extract_dt_positions(hawb_number)
    if positions is None:
        return None, {'reason': reason}

    groups = aggregate_by_tnved(positions)

    wb = Workbook()
    ws = wb.active
    ws.title = 'комакт'
    thin = Side(style='thin')
    bord = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.append(HEADERS)
    ws.append(SUBNUM)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.alignment = Alignment(wrap_text=True, vertical='top')
        c.border = bord
    for c in ws[2]:
        c.border = bord

    tot_w = Decimal('0')
    tot_v = Decimal('0')
    curr = ''
    for i, g in enumerate(groups, start=1):
        row = [
            i,
            hawb_number,
            ', '.join(g['descs']),
            (place_count if (i == 1 and place_count is not None) else ''),
            _fnum(g['gross_kg']),
            g['tnved'],
            _fnum(g['value']),
            g['currency'],
            consignee,
        ]
        ws.append(row)
        for c in ws[ws.max_row]:
            c.border = bord
            c.alignment = Alignment(wrap_text=True, vertical='top')
        tot_w += g['gross_kg']
        tot_v += g['value']
        curr = g['currency'] or curr

    ws.append(['', '', 'итого',
               (place_count if place_count is not None else ''),
               _fnum(tot_w), '', _fnum(tot_v), curr, ''])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
        c.border = bord

    for i, w in enumerate([5, 16, 60, 12, 11, 14, 16, 8, 32], start=1):
        ws.column_dimensions[chr(64 + i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    meta = {
        'consignee': consignee,
        'num_positions': len(positions),
        'num_codes': len(groups),
        'total_gross_kg': tot_w,
        'total_value': tot_v,
        'currency': curr,
        'place_count': place_count,
    }
    return buf.getvalue(), meta
