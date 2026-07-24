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

        # ⚠ Тянем id СНАЧАЛА (дёшево), потом json_extract('$.hawbs') ЧАНКАМИ по
        # id. Массовый `values_list('parsed_meta__hawbs')` заставлял SQLite
        # json_extract парсить raw_xml (4МБ) по всем строкам разом → OOM.
        # Чанк по 100-200 id держит парсинг в единицах строк за раз.
        all_ids = list(qs.values_list('id', flat=True).order_by('id'))
        self.stdout.write(f'наблюдений к обработке: {len(all_ids)}')

        existing_before = AltaOutboxWaybill.objects.count()
        self.stdout.write(f'refs в таблице сейчас: {existing_before}')

        n_obs = 0
        n_refs = 0
        chunk = opts['chunk']

        for i in range(0, len(all_ids), chunk):
            id_chunk = all_ids[i:i + chunk]
            pending: list = []
            # json_extract только на этих id — SQLite парсит parsed_meta
            # построчно, память освобождается после чанка.
            for oid, hlist in (AltaOutboxObservation.objects
                               .filter(id__in=id_chunk)
                               .values_list('id', 'parsed_meta__hawbs')):
                nums = {str(h).strip() for h in (hlist or []) if str(h).strip()}
                if not nums:
                    continue
                n_obs += 1
                for num in nums:
                    pending.append(AltaOutboxWaybill(observation_id=oid,
                                                     hawb_number=num))
            if pending and not opts['dry_run']:
                from cargo.services.alta.inbox import _retry_on_locked
                _retry_on_locked(AltaOutboxWaybill.objects.bulk_create,
                                 pending, ignore_conflicts=True,
                                 batch_size=1000, attempts=6)
            n_refs += len(pending)
            if (i // chunk) % 5 == 0:
                self.stdout.write(f'  progress: {i+len(id_chunk)}/{len(all_ids)} '
                                  f'obs_с_hawbs={n_obs} refs={n_refs}')

        existing_after = AltaOutboxWaybill.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f'ГОТОВО за {time.time()-t0:.1f}с. наблюдений с hawbs={n_obs}, '
            f'refs подготовлено={n_refs}, в таблице было={existing_before} '
            f'стало={existing_after} '
            f'{"(dry-run)" if opts["dry_run"] else ""}'))
