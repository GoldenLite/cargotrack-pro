"""Cron-команда: batch fetch ДО1 из «Декларант Плюс» для ДВ-партий.

Зеркало refresh_moscow_cargo, но через QR-сессию Декларанта и с regex-
фильтром is_far_east_candidate() вместо хардкоженных префиксов.

Driver-query:
    stage IN ('ARRIVED', 'CUSTOMS')
    AND svh_do1_reg_number = ''                       — ещё не нашли ДО1
    AND HAWB.shipment_type = 'IMPORT' (через FK)      — EXPORT не на ДВ
    AND svh_source NOT IN ('alta', 'moscow_cargo')    — не перетираем другие источники
    AND awb NE classic MAWB (regex \\d{3}-\\d{8})      — отсев Москвы/Шереметьево

Защиты:
- DEKLARANT_ENABLED=False → early return (no-op в auto_sync).
- Lockfile sync_deklarant_svh.lock (паттерн auto_sync, stale=30мин).
- session_ok() ПЕРЕД loop — без актива сразу выходим без 100 × 401.
- DeklarantAuthError в loop → mark_dead + break (НЕ continue).
- begin/end_batch_writeback вокруг loop — глушит per-cargo daemon threads.
- batch_write_svh_for_cargos в самом конце (вне транзакций).

Использование:
    manage.py sync_deklarant_svh --dry-run
    manage.py sync_deklarant_svh --limit 50 --throttle 0.5
    manage.py sync_deklarant_svh                 # full run
"""
from __future__ import annotations

import datetime
import logging
import os
import sys
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q

from cargo.models import Cargo, DeklarantSession
from cargo.services.external_warehouse.applier import (
    fetch_and_apply_deklarant, is_far_east_candidate,
)
from cargo.services.external_warehouse.deklarant import (
    DeklarantClient, DeklarantAuthError,
)


logger = logging.getLogger('cargo.external.deklarant.sync')


# Лок-файл рядом с auto_sync.lock (тот же каталог).
LOCK_DIR = os.path.join(os.path.dirname(sys.executable), '..', '..', 'tmp')
LOCK_PATH = os.path.join(os.path.abspath(LOCK_DIR), 'sync_deklarant_svh.lock')
LOCK_STALE_AFTER_SEC = 30 * 60


def _acquire_lock() -> bool:
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    if os.path.exists(LOCK_PATH):
        try:
            age = time.time() - os.path.getmtime(LOCK_PATH)
        except OSError:
            age = 0
        if age < LOCK_STALE_AFTER_SEC:
            return False
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass
    try:
        with open(LOCK_PATH, 'w') as f:
            f.write(f'pid={os.getpid()} at={datetime.datetime.now().isoformat()}\n')
        return True
    except OSError:
        return False


def _release_lock() -> None:
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except OSError:
        pass


class Command(BaseCommand):
    help = ('Batch fetch ДО1 из «Декларант Плюс» для ДВ-партий '
            '(stage ARRIVED/CUSTOMS, IMPORT, не moscow/alta).')

    def add_arguments(self, parser):
        parser.add_argument('--throttle', type=float, default=0.5,
                            help='Пауза между запросами в секундах (default 0.5).')
        parser.add_argument('--limit', type=int, default=0,
                            help='Лимит обработанных партий (0 = без лимита).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Показать кандидатов без HTTP-запросов.')

    def handle(self, *args, **opts):
        # Гейт DEKLARANT_ENABLED (паттерн как у sync_cdek_statuses).
        if not getattr(settings, 'DEKLARANT_ENABLED', False):
            self.stdout.write(self.style.WARNING(
                'DEKLARANT_ENABLED=False. Skipping sync_deklarant_svh.'))
            return

        if not _acquire_lock():
            self.stdout.write(self.style.WARNING(
                f'sync_deklarant_svh: lock уже занят ({LOCK_PATH}), skip.'))
            return

        try:
            self._run(opts)
        finally:
            _release_lock()

    def _run(self, opts):
        # 1. Сессия — проверка ДО fetch'ей, чтобы не засрать лог 100 × 401.
        session = DeklarantSession.get_active()
        if not session:
            self.stdout.write(self.style.WARNING(
                'Нет активной DeklarantSession. Запусти: manage.py deklarant_login'))
            return

        # 2. Driver-query: то что точно надо проверить.
        # shipment_type живёт на HouseWaybill, выходим через hawbs__shipment_type='IMPORT'.
        qs = (Cargo.objects
              .filter(stage__in=('ARRIVED', 'CUSTOMS'))
              .filter(
                  Q(svh_do1_reg_number='') | Q(svh_do1_reg_number__isnull=True))
              .filter(hawbs__shipment_type='IMPORT')
              .exclude(svh_source__in=('alta', 'moscow_cargo'))
              .exclude(awb_number='')
              .distinct()
              .order_by('-created_at'))

        # 3. Python-side regex (SQLite REGEXP не всегда зарегистрирован).
        candidates: list[Cargo] = []
        for cargo in qs.iterator():
            if not is_far_east_candidate(cargo):
                continue
            candidates.append(cargo)
            if opts['limit'] and len(candidates) >= opts['limit']:
                break

        self.stdout.write(
            f'Кандидаты: {len(candidates)} (ДВ, ARRIVED/CUSTOMS, IMPORT, '
            f'не alta/moscow_cargo, не классический MAWB)')

        if opts['dry_run']:
            for c in candidates[:30]:
                self.stdout.write(
                    f'  {c.awb_number}  (stage={c.stage}, svh_source={c.svh_source!r})')
            if len(candidates) > 30:
                self.stdout.write(f'  ... и ещё {len(candidates) - 30}')
            return

        if not candidates:
            return

        # 4. Сетевой санити: дешёвый session_ok ДО первого fetch.
        try:
            with DeklarantClient.from_db() as probe:
                if not probe:
                    self.stdout.write(self.style.ERROR(
                        'Не удалось открыть DeklarantClient (нет сессии в БД).'))
                    return
                if not probe.session_ok():
                    session.mark_dead('session_ok() returned False at sync start')
                    self.stdout.write(self.style.ERROR(
                        'session_ok() == False. Сессия помечена мёртвой. '
                        'Нужен новый QR-логин.'))
                    return
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Probe session_ok failed: {e}'))
            return

        # 5. Главный loop с supression daemon threads writeback.
        from cargo.services.sheets.writeback import (
            begin_batch_writeback, end_batch_writeback,
            batch_write_svh_for_cargos,
        )

        n_applied = n_empty = n_skipped = n_error = n_auth_error = 0
        applied_cargos: list[Cargo] = []

        begin_batch_writeback()
        try:
            with DeklarantClient.from_db() as client:
                if not client:
                    self.stdout.write(self.style.ERROR(
                        'DeklarantClient.from_db() == None — сессия пропала между probe и loop?'))
                    return
                for i, cargo in enumerate(candidates, 1):
                    try:
                        result = fetch_and_apply_deklarant(
                            cargo, client=client, writeback=False)
                    except DeklarantAuthError as e:
                        n_auth_error += 1
                        session.mark_dead(f'sync_deklarant_svh: {e}')
                        self.stdout.write(self.style.ERROR(
                            f'  {cargo.awb_number}: DeklarantAuthError, '
                            f'сессия помечена мёртвой. Abort loop.'))
                        break
                    except Exception as e:
                        n_error += 1
                        logger.exception('deklarant sync error for %s', cargo.awb_number)
                        self.stdout.write(self.style.WARNING(
                            f'  {cargo.awb_number}: {type(e).__name__}: {e}'))
                        continue

                    if result is True:
                        n_applied += 1
                        applied_cargos.append(cargo)
                        self.stdout.write(self.style.SUCCESS(
                            f'  {cargo.awb_number} → ДО1 '
                            f'{cargo.svh_do1_reg_number or "(?)"}'))
                    elif result is False:
                        n_skipped += 1
                    else:
                        n_empty += 1

                    if i % 20 == 0:
                        self.stdout.write(
                            f'  progress: {i}/{len(candidates)}  '
                            f'applied={n_applied} empty={n_empty} '
                            f'skip={n_skipped} err={n_error}')

                    if opts['throttle'] and i < len(candidates):
                        time.sleep(opts['throttle'])
        finally:
            end_batch_writeback()

        self.stdout.write(self.style.SUCCESS(
            f'Deklarant sync done. processed={len(candidates)} '
            f'applied={n_applied} empty={n_empty} skipped={n_skipped} '
            f'errors={n_error} auth_errors={n_auth_error}'))

        # 6. Финальный batch writeback в Sheets (вне любых транзакций).
        if applied_cargos:
            self.stdout.write('')
            self.stdout.write(
                f'Sheets writeback для {len(applied_cargos)} партий...')
            try:
                cells = batch_write_svh_for_cargos(applied_cargos)
                self.stdout.write(self.style.SUCCESS(
                    f'Sheets done. cells_written={cells}'))
            except Exception as e:
                logger.exception('sync_deklarant_svh: sheets writeback failed')
                self.stdout.write(self.style.ERROR(
                    f'Sheets writeback failed: {e}'))
