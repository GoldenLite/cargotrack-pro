"""Массовый refresh партий с префиксами Шереметьево-Карго (shercargo.ru).

Структурно зеркало refresh_moscow_cargo — но для другого терминала.
Берёт Cargo с awb_number начинающимся на префиксы из SHERCARGO_PREFIXES
(только авиа, формат XXX-XXXXXXXX), у которых пустой `svh_do1_reg_number`.

По каждой делает GET к /w/pls/pub/www_pub.awb_info → если ДО1 уже
зарегистрирован, применяет и пишет в Sheets одним batch.

Throttle: 1 сек между запросами по умолчанию — вежливо к чужому сайту.

Запуск (для cron):
    uv run python manage.py refresh_shercargo
    uv run python manage.py refresh_shercargo --dry-run
    uv run python manage.py refresh_shercargo --throttle 0.5 --limit 50
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db.models import Q

from cargo.models import Cargo
from cargo.services.external_warehouse.applier import (
    SHERCARGO_PREFIXES, apply_to_cargo, _save_with_retry,
)
from cargo.services.external_warehouse.shercargo import ShercargoClient


class Command(BaseCommand):
    help = 'Batch fetch ДО1-инфы с shercargo.ru для подходящих партий'

    def add_arguments(self, parser):
        parser.add_argument('--throttle', type=float, default=1.0,
                            help='Пауза между запросами в секундах (default 1.0)')
        parser.add_argument('--limit', type=int, default=0,
                            help='Лимит обработанных партий (для теста)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Только показать кандидатов, без HTTP')
        parser.add_argument('--include-filled', action='store_true',
                            help='Не пропускать партии с уже заполненным svh_do1_reg_number')

    def handle(self, *args, **opts):
        prefix_q = Q()
        for p in SHERCARGO_PREFIXES:
            prefix_q |= Q(awb_number__startswith=f'{p}-')
        qs = Cargo.objects.filter(prefix_q)

        if not opts['include_filled']:
            qs = qs.filter(Q(svh_do1_reg_number='') | Q(svh_do1_reg_number__isnull=True))

        if opts['limit']:
            qs = qs[:opts['limit']]

        cargos = list(qs)
        self.stdout.write(
            f'Кандидаты: {len(cargos)} (префиксы={SHERCARGO_PREFIXES})'
        )

        if opts['dry_run']:
            for c in cargos[:30]:
                self.stdout.write(f'  {c.awb_number}')
            if len(cargos) > 30:
                self.stdout.write(f'  ... и ещё {len(cargos) - 30}')
            return

        if not cargos:
            return

        n_found = n_applied = n_empty = n_error = 0
        applied_cargos: list = []
        with ShercargoClient() as client:
            for i, cargo in enumerate(cargos, 1):
                try:
                    parsed = client.fetch(cargo.awb_number)
                except Exception as e:
                    n_error += 1
                    self.stdout.write(self.style.ERROR(
                        f'  {cargo.awb_number}: {e}'))
                    continue

                if not parsed:
                    n_empty += 1
                else:
                    n_found += 1
                    if apply_to_cargo(cargo, parsed, writeback=False):
                        n_applied += 1
                        applied_cargos.append(cargo)
                        if not (cargo.svh_source or '').strip():
                            cargo.svh_source = 'shercargo'
                            try:
                                _save_with_retry(cargo, ['svh_source'])
                            except Exception:
                                pass
                        self.stdout.write(self.style.SUCCESS(
                            f'  {cargo.awb_number}: {parsed.get("reg_number")} '
                            f'({parsed.get("do1_date")})'
                        ))

                if i % 20 == 0:
                    self.stdout.write(
                        f'  progress: {i}/{len(cargos)} '
                        f'found={n_found} applied={n_applied} '
                        f'empty={n_empty} err={n_error}'
                    )

                if opts['throttle'] and i < len(cargos):
                    time.sleep(opts['throttle'])

        self.stdout.write(self.style.SUCCESS(
            f'BD update done. processed={len(cargos)} found={n_found} '
            f'applied={n_applied} no_do1_yet={n_empty} errors={n_error}'
        ))

        if not applied_cargos:
            return

        # Batch Sheets writeback — переиспользуем общий хелпер.
        self.stdout.write(f'\nSheets writeback для {len(applied_cargos)} партий...')
        from cargo.services.sheets.writeback import batch_write_svh_for_cargos
        try:
            cells = batch_write_svh_for_cargos(applied_cargos)
            self.stdout.write(self.style.SUCCESS(
                f'Sheets done. cells_written={cells}'
            ))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Sheets writeback failed: {e}'))
