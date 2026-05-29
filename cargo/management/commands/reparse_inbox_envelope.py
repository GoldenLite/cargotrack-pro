"""Пере-парс inbox сообщений где msg_type — конвертный (ED.11010) но
app:MessageKind содержит реальный бизнес-тип (CMN.11001, ...).

После апдейта парсера parse_raw_xml использует MessageKind приоритетнее.
Эта команда:
1. Находит все inbox-msgs с msg_type='ED.11010' (или другие envelope-типы).
2. Перечитывает raw_xml через parse_raw_xml.
3. Если новый msg_type отличается → обновляет msg_type, parsed_meta,
   msg_kind через classify(), и вызывает dispatch чтобы применить
   статусы/декларации.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage
from cargo.services.alta.inbox import classify, dispatch
from cargo.services.alta.xml_extract import parse_raw_xml


ENVELOPE_TYPES = ('ED.11010',)


class Command(BaseCommand):
    help = 'Пере-парс inbox-сообщений с envelope msg_type (ED.11010).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--limit', type=int, default=0)

    def handle(self, *args, **opts):
        from cargo.services.sheets.writeback import (
            begin_batch_writeback, end_batch_writeback,
        )

        qs = AltaInboxMessage.objects.filter(
            msg_type__in=ENVELOPE_TYPES).order_by('prepared_at')
        if opts['limit']:
            qs = qs[:opts['limit']]
        total = qs.count() if hasattr(qs, 'count') else len(list(qs))
        self.stdout.write(f'Кандидатов: {total}')

        changed = 0
        ok_dispatch = 0
        err_dispatch = 0

        if not opts['dry_run']:
            begin_batch_writeback()
        try:
            for m in qs:
                raw = m.raw_xml or ''
                if not raw:
                    continue
                new_meta = parse_raw_xml(raw)
                new_type = (new_meta.get('msg_type') or '').strip()
                if not new_type or new_type == m.msg_type:
                    continue

                if opts['dry_run']:
                    if changed < 30:
                        self.stdout.write(
                            f'  #{m.pk}  {m.msg_type} → {new_type}  '
                            f'gtd={new_meta.get("gtd_number")!r}  '
                            f'hawbs={new_meta.get("providing_hawbs")}')
                    changed += 1
                    continue

                m.msg_type = new_type
                # parsed_meta слиянием: новый extract заменяет старый
                merged = dict(m.parsed_meta or {})
                merged.update(new_meta)
                m.parsed_meta = merged
                m.msg_kind = classify(new_type, merged)
                m.save(update_fields=['msg_type', 'parsed_meta', 'msg_kind'])
                changed += 1

                try:
                    dispatch(m)
                    ok_dispatch += 1
                except Exception as e:
                    err_dispatch += 1
                    if err_dispatch < 5:
                        self.stdout.write(f'  dispatch ERR #{m.pk}: {e}')
        finally:
            if not opts['dry_run']:
                end_batch_writeback()

        self.stdout.write(self.style.SUCCESS(
            f'Изменено типов: {changed}, dispatched OK={ok_dispatch} '
            f'ERR={err_dispatch}'))
