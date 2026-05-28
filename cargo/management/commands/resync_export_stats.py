"""Резинк всех экспортных данных в Sheets «Экспортная статистика».

Что делает:
1. Re-парсит все AltaOutboxObservation типов CMN.11335/11349/11024 с
   raw_xml — определяет ЭК/ИМ через DeclarationKindCode / CustomsProcedure.
2. Для каждого ЭК-сообщения: auto-create HAWB(EXPORT) + Cargo, проставляет
   filed_date / goods_count / declaration_form, добавляет строку в Sheets и
   запускает batch writeback всех экспортных колонок.

Запуск:
    uv run python manage.py resync_export_stats           # обработать всё
    uv run python manage.py resync_export_stats --limit=10    # первые N сообщений
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import AltaOutboxObservation
from cargo.services.alta.outbox import _apply_export_outbox


class Command(BaseCommand):
    help = 'Перепроход по экспортным CMN.11335/11349/11024 + writeback в Sheets'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0,
                            help='Обработать первые N сообщений (0 = все)')
        parser.add_argument('--msg-types', nargs='+',
                            default=['CMN.11335', 'CMN.11349', 'CMN.11024'])

    def handle(self, *args, **opts):
        qs = (AltaOutboxObservation.objects
              .filter(msg_type__in=opts['msg_types'])
              .order_by('-prepared_at'))
        if opts['limit']:
            qs = qs[:opts['limit']]

        total = qs.count() if hasattr(qs, 'count') else len(list(qs))
        self.stdout.write(f'Сообщений для обработки: {total}')

        processed = 0
        skipped_no_xml = 0
        skipped_im = 0
        for obs in qs:
            raw = (obs.parsed_meta or {}).get('raw_xml') or ''
            if not raw:
                skipped_no_xml += 1
                continue
            # _apply_export_outbox сам проверит is_export и пропустит ИМ
            _apply_export_outbox(obs)
            processed += 1
            if processed % 50 == 0:
                self.stdout.write(f'  обработано {processed}/{total}')

        self.stdout.write(self.style.SUCCESS(
            f'\nГотово. Обработано: {processed}, без raw_xml: {skipped_no_xml}'))
