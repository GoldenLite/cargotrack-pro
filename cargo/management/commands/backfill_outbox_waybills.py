# -*- coding: utf-8 -*-
"""Разовый бэкфилл AltaOutboxWaybill из parsed_meta['hawbs'] существующих
исходящих наблюдений.

Денормализация (23.07.2026): выносим список накладных из parsed_meta (где
рядом raw_xml до 4МБ) в индексированную таблицу. parsed_meta НЕ трогаем.

⚠ Тянем ТОЛЬКО json_extract('$.hawbs') через values_list('parsed_meta__hawbs'),
а НЕ весь parsed_meta — иначе raw_xml раздувает память. Идёт чанками, чтобы
на многотысячной выборке SQLite не аллоцировал результат целиком.

Идемпотентна: sync-семантика (приводит refs к текущему hawbs). Повторный
прогон безопасен. Обрабатывает только типы-подачи + ED.DO1 (у них есть hawbs).
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from cargo.models import AltaOutboxObservation, AltaOutboxWaybill


# Типы, где в parsed_meta есть список hawbs (подачи + ДО1).
HAWBS_TYPES = ('CMN.11349', 'CMN.11023', 'CMN.11335', 'CMN.11024', 'ED.DO1')


class Command(BaseCommand):
    help = 'Бэкфилл денормализованной таблицы AltaOutboxWaybill из parsed_meta'

    def add_arguments(self, parser):
        parser.add_argument('--chunk', type=int, default=500,
                            help='Размер чанка выборки (default 500)')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--all-types', action='store_true',
                            help='Не ограничивать msg_type (по умолчанию только '
                                 'типы с hawbs)')

    def handle(self, *args, **opts):
        t0 = time.time()
        qs = AltaOutboxObservation.objects.all()
        if not opts['all_types']:
            qs = qs.filter(msg_type__in=HAWBS_TYPES)

        # Тянем ТОЛЬКО id + json_extract('$.hawbs'), без parsed_meta целиком.
        ids_hawbs = list(
            qs.values_list('id', 'parsed_meta__hawbs').order_by('id'))
        self.stdout.write(f'наблюдений к обработке: {len(ids_hawbs)}')

        existing_before = AltaOutboxWaybill.objects.count()
        self.stdout.write(f'refs в таблице сейчас: {existing_before}')

        n_obs = 0
        n_refs = 0
        pending: list = []
        chunk = opts['chunk']

        def _flush():
            nonlocal pending, n_refs
            if pending and not opts['dry_run']:
                AltaOutboxWaybill.objects.bulk_create(
                    pending, ignore_conflicts=True, batch_size=1000)
            n_refs += len(pending)
            pending = []

        for oid, hlist in ids_hawbs:
            nums = {str(h).strip() for h in (hlist or []) if str(h).strip()}
            if not nums:
                continue
            n_obs += 1
            for num in nums:
                pending.append(AltaOutboxWaybill(observation_id=oid,
                                                 hawb_number=num))
            if len(pending) >= chunk:
                _flush()
        _flush()

        existing_after = AltaOutboxWaybill.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f'ГОТОВО за {time.time()-t0:.1f}с. наблюдений с hawbs={n_obs}, '
            f'refs подготовлено={n_refs}, в таблице было={existing_before} '
            f'стало={existing_after} '
            f'{"(dry-run)" if opts["dry_run"] else ""}'))
