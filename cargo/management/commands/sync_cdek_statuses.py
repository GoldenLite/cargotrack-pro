"""Reconcile/backfill статусов СДЭК — safety-net на пропущенные вебхуки.

Основной канал апдейтов — вебхуки ORDER_STATUS. Эта команда добирает то,
что вебхук мог не доставить: проходит по HAWB с известным заказом СДЭК
(есть cdek_uuid/cdek_number), который ещё НЕ в терминальном статусе, и
перезапрашивает статус по im_number (=hawb_number).

Throttle между запросами — вежливо к API. Одна сессия (токен переиспользуется).

    uv run python manage.py sync_cdek_statuses
    uv run python manage.py sync_cdek_statuses --dry-run --limit 5
    uv run python manage.py sync_cdek_statuses --include-unsynced   # + зонд по поздним статусам
"""
from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q

from cargo.models import HouseWaybill
from cargo.services.cdek.client import CDEK_TERMINAL_CODES


# Логистические статусы, на которых уместна доставка СДЭК — для --include-unsynced.
_DELIVERY_STAGES = (
    'READY_DELIVERY', 'TO_SORT_CENTER', 'AT_SORT_CENTER',
    'READY_TO_DEST', 'IN_TRANSIT_DEST', 'ARRIVED_FINAL',
)


class Command(BaseCommand):
    help = 'Reconcile статусов доставки СДЭК (safety-net на пропущенные вебхуки)'

    def add_arguments(self, parser):
        parser.add_argument('--throttle', type=float, default=0.5,
                            help='Пауза между запросами, сек (default 0.5)')
        parser.add_argument('--limit', type=int, default=0,
                            help='Лимит HAWB (для теста)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Только показать кандидатов, без HTTP')
        parser.add_argument('--include-unsynced', action='store_true',
                            help='Также зондировать HAWB без cdek_* в поздних '
                                 'логистических статусах (по hawb_number=im_number)')

    def handle(self, *args, **opts):
        if not getattr(settings, 'CDEK_ENABLED', False):
            self.stdout.write('CDEK_ENABLED=false — пропускаю.')
            return

        # Кандидаты: известен заказ СДЭК и он ещё не терминальный.
        known = (
            HouseWaybill.objects
            .filter(Q(cdek_uuid__gt='') | Q(cdek_number__gt=''))
            .exclude(cdek_status_code__in=CDEK_TERMINAL_CODES)
        )
        hawbs = list(known)

        if opts['include_unsynced']:
            unsynced = (
                HouseWaybill.objects
                .filter(cdek_uuid='', cdek_number='',
                        logistics_status__in=_DELIVERY_STAGES)
            )
            hawbs += list(unsynced)

        if opts['limit']:
            hawbs = hawbs[:opts['limit']]

        self.stdout.write(f'Кандидаты: {len(hawbs)}')
        if opts['dry_run']:
            for h in hawbs[:30]:
                self.stdout.write(f'  {h.hawb_number} '
                                  f'(cdek={h.cdek_number or "—"} '
                                  f'status={h.cdek_status_code or "—"})')
            if len(hawbs) > 30:
                self.stdout.write(f'  ... и ещё {len(hawbs) - 30}')
            return

        if not hawbs:
            return

        from cargo.services.cdek.client import CdekClient
        from cargo.services.cdek import applier as cdek_applier

        n_updated = n_actual = n_none = n_error = 0
        with CdekClient() as client:
            for i, hawb in enumerate(hawbs, 1):
                try:
                    res = cdek_applier.fetch_and_apply(hawb, client=client,
                                                       source='poll')
                except Exception as e:
                    n_error += 1
                    self.stdout.write(self.style.ERROR(f'  {hawb.hawb_number}: {e}'))
                    continue

                if res is None:
                    n_none += 1
                elif res:
                    n_updated += 1
                    self.stdout.write(self.style.SUCCESS(
                        f'  {hawb.hawb_number}: {hawb.cdek_status_display}'))
                else:
                    n_actual += 1

                if i % 20 == 0:
                    self.stdout.write(
                        f'  progress: {i}/{len(hawbs)} updated={n_updated} '
                        f'actual={n_actual} none={n_none} err={n_error}')

                if opts['throttle'] and i < len(hawbs):
                    time.sleep(opts['throttle'])

        self.stdout.write(self.style.SUCCESS(
            f'Done. processed={len(hawbs)} updated={n_updated} '
            f'no_change={n_actual} not_found={n_none} errors={n_error}'))
