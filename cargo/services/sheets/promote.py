"""Создание HouseWaybill из ImportedSheetRow (orphan → promoted).

Используется и single-promote (кнопка на drill-down), и bulk-promote
(чекбоксы на индексе). Дёрнем CRM-rematch после promote, чтобы события
workflow появились автоматически.
"""
from __future__ import annotations

from typing import Optional

from django.contrib.auth.models import User
from django.utils import timezone

from cargo.models import (
    HouseWaybill,
    ImportedSheetRow,
    SheetUserAlias,
    Warehouse,
)

from .events import emit_workflow_events
from .matcher import match_row
from .mapping import (
    GEN_BOND_DATE,
    GEN_COMMENT,
    GEN_DECLARATION,
    GEN_PROBLEM,
    GEN_RELEASE_TYPE,
    GEN_RESPONSIBLE,
    GEN_TSD,
    GEN_VED_MANAGER,
    GEN_WAREHOUSE_LIC,
    map_release_type,
    normalize_inn,
    parse_date_safe,
)


def _resolve_user(alias_text: str, role_hint: str) -> Optional[User]:
    """ФИО из Sheets → User через SheetUserAlias."""
    if not alias_text:
        return None
    a = (
        SheetUserAlias.objects
        .filter(alias__iexact=alias_text.strip(), user__is_active=True)
        .select_related('user')
        .first()
    )
    return a.user if a else None


def _resolve_warehouse(license_text: str) -> Optional[Warehouse]:
    """«Лицензия СВХ (фактическое местонахождение)» → Warehouse.

    Поле в Sheets выглядит как «10005/181213/10047/9(МК старый)» —
    лицензия + опциональный комментарий в скобках. Берём префикс
    до первой скобки/пробела.
    """
    if not license_text:
        return None
    lic = license_text.split('(', 1)[0].strip()
    if not lic:
        return None
    return (
        Warehouse.objects
        .filter(license_number=lic)
        .first()
    )


def promote_row(row: ImportedSheetRow, *, user: Optional[User] = None) -> HouseWaybill:
    """Создаёт HAWB из orphan-строки «Общее» и связывает её обратно.

    Бросает ValueError, если строка не orphan или kind != general.
    """
    if row.match_status != 'orphan':
        raise ValueError(
            f'Promote доступен только для orphan-строк (сейчас: {row.match_status})'
        )
    if row.source.kind != 'general':
        raise ValueError('Promote разрешён только для «Общее» источника')
    if not row.hawb_number_norm:
        raise ValueError('У строки пустой hawb_number_norm — нечем нумеровать HAWB')

    data = row.data or {}
    cargo_type = map_release_type(data.get(GEN_RELEASE_TYPE) or '') or 'B2C'

    bond_dt = parse_date_safe(data.get(GEN_BOND_DATE) or '')
    warehouse = _resolve_warehouse(data.get(GEN_WAREHOUSE_LIC) or '')
    assigned = _resolve_user(data.get(GEN_RESPONSIBLE) or '', 'declarant')
    ved      = _resolve_user(data.get(GEN_VED_MANAGER) or '', 'ved_manager')

    hawb = HouseWaybill.objects.create(
        hawb_number=row.hawb_number_norm,
        cargo_type=cargo_type,
        consignee_inn=normalize_inn(data.get('ТО Клиент') or ''),
        problem_note=(data.get(GEN_PROBLEM) or '')[:5000],
        tsd_number=(data.get(GEN_TSD) or '')[:64],
        customs_declaration_number=(data.get(GEN_DECLARATION) or '')[:50],
        notes=(data.get(GEN_COMMENT) or '')[:5000],
        warehouse=warehouse,
        assigned_to=assigned,
        ved_manager=ved,
        scan_into_bond=bond_dt,
        logistics_status='CREATED',
    )

    row.match_status = 'promoted'
    row.matched_hawb = hawb
    row.promoted_hawb = hawb
    row.save(update_fields=['match_status', 'matched_hawb', 'promoted_hawb'])

    # Авто-сматчить CRM-строки с тем же номером, чтобы события появились сразу
    for crm_row in ImportedSheetRow.objects.filter(
        source__kind='crm',
        hawb_number_norm=row.hawb_number_norm,
    ).exclude(pk=row.pk):
        match_row(crm_row)
        crm_row.save()
        if crm_row.matched_hawb_id:
            emit_workflow_events(crm_row)

    return hawb
