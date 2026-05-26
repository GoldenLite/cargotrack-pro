"""Синхронизирует filed_date по всем уникальным ДТ.

Берёт все HouseWaybill у которых стоит customs_declaration_number,
группирует по ДТ. Для каждой группы — если есть HAWB с filed_date,
проставляет min(filed_date) ВСЕМ HAWB этой ДТ. Это закрывает гонку
сообщений: CMN.11023/11349 (исходящее с filed_date) пришло ДО CMN.11350
(которое присваивает ДТ) — у одной HAWB filed_date есть, у остальных
ДТ-siblings пусто. Команда «дотягивает».

Идемпотентно. Запускается в auto_sync.

Запуск:
    uv run python manage.py sync_filed_dates
    uv run python manage.py sync_filed_dates --dry-run
"""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db.models import Q

from cargo.models import HouseWaybill
from cargo.services.alta.inbox import _sync_filed_date_by_declaration


class Command(BaseCommand):
    help = 'Синхронизирует filed_date по всем ДТ (min по группе)'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        # Все уникальные ДТ где есть HAWB с filed_date — нам важны только
        # такие группы, остальные пропускаем (нечего «дотягивать»).
        decls_with_data = (
            HouseWaybill.objects
            .exclude(customs_declaration_number='')
            .filter(filed_date__isnull=False)
            .values_list('customs_declaration_number', flat=True)
            .distinct()
        )
        decls = list(decls_with_data)
        self.stdout.write(f'Уникальных ДТ с filed_date: {len(decls)}')

        if opts['dry_run']:
            # Покажем какие ДТ имеют расхождения
            from collections import Counter
            need_sync = []
            for decl in decls:
                hawbs = list(HouseWaybill.objects.filter(
                    customs_declaration_number=decl
                ).only('filed_date'))
                if len(hawbs) < 2:
                    continue
                dates = {h.filed_date for h in hawbs}
                # None считается отдельно
                non_null = [d for d in dates if d]
                if not non_null:
                    continue
                if len(dates) > 1:  # есть разница (либо разные даты, либо None)
                    need_sync.append((decl, len(hawbs),
                                       len([h for h in hawbs if h.filed_date is None])))
            self.stdout.write(f'ДТ требуют sync: {len(need_sync)}')
            for decl, total, nulls in need_sync[:20]:
                self.stdout.write(f'  {decl}: {total} HAWB, у {nulls} filed_date=None')
            if len(need_sync) > 20:
                self.stdout.write(f'  ... ещё {len(need_sync)-20}')
            return

        # Боевой режим — sync по каждой ДТ
        synced = 0
        for decl in decls:
            try:
                _sync_filed_date_by_declaration(decl)
                synced += 1
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f'  ДТ {decl}: {e}'))
        self.stdout.write(self.style.SUCCESS(
            f'Прошло sync по ДТ: {synced}'))
        # NB: propagate по Cargo НЕ делаем — юзер: «должна быть дата подачи
        # только у тех HAWB которые в CMN.11349». Если CMN.11349 содержит
        # одну HAWB — у одной дата. Если содержит 12 — нужно парсить список
        # из raw_xml и dispatch'ить filed_date каждой (см. outbox.dispatch).
