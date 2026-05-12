"""Эмиссия HawbWorkflowEvent из CRM-строк."""
from __future__ import annotations

from django.utils import timezone

from cargo.models import HawbWorkflowEvent, ImportedSheetRow

from .mapping import (
    CRM_COMMENT_MAP,
    CRM_EVENT_MAP,
    is_truthy_marker,
    parse_date_safe,
)


def emit_workflow_events(row: ImportedSheetRow) -> int:
    """По строке CRM-таблицы создаёт/обновляет события workflow.

    Возвращает количество созданных или обновлённых событий.
    Идемпотентность гарантируется unique_together = (hawb, event_type, source_row).
    Если строка ещё не сматчена с HAWB — ничего не делает.
    """
    if not row.matched_hawb_id:
        return 0
    data = row.data or {}
    if not data:
        return 0

    # Сначала собираем комментарии «событие → текст»
    comments: dict[str, str] = {}
    for col, event_type in CRM_COMMENT_MAP.items():
        text = data.get(col)
        if text:
            comments[event_type] = str(text).strip()

    touched = 0
    now = timezone.now()

    for col, event_type in CRM_EVENT_MAP.items():
        raw = data.get(col)
        if raw is None or str(raw).strip() == '':
            continue

        parsed = parse_date_safe(raw)
        if parsed is not None:
            occurred_at = parsed
            raw_value = ''
        elif is_truthy_marker(raw):
            occurred_at = now
            raw_value = str(raw).strip()[:255]
        else:
            # неструктурированное значение — кладём как есть, дата = run.now
            occurred_at = now
            raw_value = str(raw).strip()[:255]

        comment = comments.get(event_type, '')

        _, _created = HawbWorkflowEvent.objects.update_or_create(
            hawb=row.matched_hawb,
            event_type=event_type,
            source_row=row,
            defaults={
                'occurred_at': occurred_at,
                'comment': comment,
                'raw_value': raw_value,
                'source': 'sheet',
            },
        )
        touched += 1

    return touched
