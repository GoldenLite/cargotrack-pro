"""Матчинг ImportedSheetRow → HouseWaybill + расчёт diff_summary."""
from __future__ import annotations

from typing import Optional

from cargo.models import HouseWaybill, ImportedSheetRow

from .mapping import (
    GEN_CLIENT_INN,
    GEN_COMMENT,
    GEN_DECLARATION,
    GEN_HAWB_NUMBER,
    GEN_PROBLEM,
    GEN_RELEASE_TYPE,
    GEN_RESPONSIBLE,
    GEN_TSD,
    GEN_VED_MANAGER,
    map_release_type,
    normalize_hawb_number,
    normalize_inn,
)


def _value(row_data: dict, key: str) -> str:
    """Безопасно достаём строку из data, обрезаем по краям."""
    v = row_data.get(key)
    if v is None:
        return ''
    return str(v).strip()


def extract_keys(row: ImportedSheetRow) -> None:
    """Вынимает в denormalized-поля все ключевые идентификаторы."""
    data = row.data or {}
    raw_hawb = _value(data, GEN_HAWB_NUMBER)
    row.hawb_number_raw  = raw_hawb[:64]
    row.hawb_number_norm = normalize_hawb_number(raw_hawb)[:64]
    row.inn_raw          = normalize_inn(_value(data, GEN_CLIENT_INN))[:32]
    row.declaration_number = _value(data, GEN_DECLARATION)[:64]


def match_row(row: ImportedSheetRow) -> None:
    """Заполняет row.match_status / matched_hawb / diff_summary in-place.

    Не вызывает .save() — сохраняет caller.
    """
    extract_keys(row)

    if not row.hawb_number_norm:
        row.match_status = 'ambiguous'
        row.matched_hawb = None
        row.matched_cargo = None
        row.diff_summary = {'_reason': 'no_hawb_number_in_row'}
        return

    candidates = list(
        HouseWaybill.objects
        .select_related('mawb')
        .filter(hawb_number__iexact=row.hawb_number_norm)
    )

    if not candidates:
        row.match_status = 'orphan'
        row.matched_hawb = None
        row.matched_cargo = None
        row.diff_summary = {}
        return

    if len(candidates) > 1:
        row.match_status = 'conflict'
        row.matched_hawb = None
        row.matched_cargo = None
        row.diff_summary = {
            '_candidates': [
                {'id': h.id, 'mawb': h.mawb.awb_number if h.mawb else None}
                for h in candidates
            ]
        }
        return

    hawb = candidates[0]
    row.match_status  = 'matched'
    row.matched_hawb  = hawb
    row.matched_cargo = hawb.mawb
    row.diff_summary  = compute_diff(row.data or {}, hawb)


def compute_diff(data: dict, hawb: HouseWaybill) -> dict:
    """Сравнивает значения из Sheets со значениями в БД. Возвращает dict расхождений."""
    diff: dict = {}

    def _pair(field_name: str, sheet_val, db_val):
        sheet_s = '' if sheet_val is None else str(sheet_val).strip()
        db_s    = '' if db_val is None else str(db_val).strip()
        if sheet_s != db_s:
            diff[field_name] = {'sheet': sheet_s, 'db': db_s}

    # ИНН
    sheet_inn = normalize_inn(_value(data, GEN_CLIENT_INN))
    _pair('consignee_inn', sheet_inn, hawb.consignee_inn)

    # Тип выпуска → cargo_type
    sheet_kind = map_release_type(_value(data, GEN_RELEASE_TYPE))
    if sheet_kind:
        _pair('cargo_type', sheet_kind, hawb.cargo_type)

    # № ДТ
    _pair('customs_declaration_number',
          _value(data, GEN_DECLARATION),
          hawb.customs_declaration_number)

    # ТСД
    _pair('tsd_number', _value(data, GEN_TSD), hawb.tsd_number)

    # Проблема
    _pair('problem_note', _value(data, GEN_PROBLEM), hawb.problem_note)

    # Комментарий
    _pair('notes', _value(data, GEN_COMMENT), hawb.notes)

    # Имена (без резолва в User — пока просто фиксируем расхождение текстом)
    sheet_responsible = _value(data, GEN_RESPONSIBLE)
    db_responsible = hawb.assigned_to.get_full_name() if hawb.assigned_to else ''
    if sheet_responsible:
        _pair('assigned_to_name', sheet_responsible, db_responsible)

    sheet_ved = _value(data, GEN_VED_MANAGER)
    db_ved = hawb.ved_manager.get_full_name() if hawb.ved_manager else ''
    if sheet_ved:
        _pair('ved_manager_name', sheet_ved, db_ved)

    return diff
