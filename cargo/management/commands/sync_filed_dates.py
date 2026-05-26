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

        # Дополнительно — propagate по Cargo для HAWB у которых filed_date
        # стоит, а customs_declaration_number пустой (CMN.11350 от таможни
        # ещё не пришло). Логически: одна декларация на партию = filed_date
        # для всех HAWB партии. Когда придёт CMN.11350 и присвоит ДТ —
        # обычный sync по ДТ перепишет даты на min по группе ДТ.
        cargo_synced = self._sync_filed_dates_by_cargo()
        self.stdout.write(self.style.SUCCESS(
            f'Прошло sync по Cargo (для HAWB без ДТ): {cargo_synced}'))

    def _sync_filed_dates_by_cargo(self) -> int:
        """Для Cargo у которых хотя бы 1 HAWB имеет filed_date но customs_decl
        пустой — разнести filed_date на siblings без filed_date.
        """
        from cargo.models import Cargo
        from cargo.services.sheets.writeback import (
            batch_write_filed_dates_for_hawbs, signals_suppressed,
        )

        # Cargo у которых хотя бы один HAWB c filed_date + customs_decl='' —
        # кандидаты на propagate.
        cargo_ids = (
            HouseWaybill.objects
            .filter(filed_date__isnull=False, customs_declaration_number='')
            .values_list('mawb_id', flat=True).distinct()
        )
        cargo_ids = [pk for pk in cargo_ids if pk]
        count = 0
        all_affected: list = []
        for cid in cargo_ids:
            hawbs = list(HouseWaybill.objects.filter(mawb_id=cid).only(
                'pk', 'filed_date', 'customs_declaration_number'))
            # Минимальный filed_date по партии (только тех у кого нет ДТ —
            # т.е. мы ещё не получили CMN.11350; для тех у кого ДТ есть,
            # сработает sync_by_declaration)
            dates_no_decl = [h.filed_date for h in hawbs
                             if h.filed_date and not h.customs_declaration_number]
            if not dates_no_decl:
                continue
            min_date = min(dates_no_decl)
            # Affected: HAWB без filed_date и без ДТ
            affected = [h for h in hawbs
                        if h.filed_date is None and not h.customs_declaration_number]
            if not affected:
                continue
            pks = [h.pk for h in affected]
            HouseWaybill.objects.filter(pk__in=pks).update(filed_date=min_date)
            all_affected.extend(affected)
            count += 1

        # Writeback в Sheets единым batch'ом
        if all_affected and not signals_suppressed():
            for h in all_affected:
                h.refresh_from_db(fields=['filed_date'])
            try:
                batch_write_filed_dates_for_hawbs(all_affected)
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f'Sheets writeback failed: {e}'))
        return count
