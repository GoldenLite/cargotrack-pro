"""Авто-синхронизация Sheets ↔ БД по расписанию.

Вызывает по очереди:
  1. import_sheets --source general    — обновить row_idx + data
  2. relink_hawbs_from_tsd --all       — привязать HAWB по ТСД
  3. refresh_moscow_cargo              — подтянуть MC-данные с moscow-cargo.com
  4. audit_sheets_vs_db --fix          — затереть стейл

Опция --full дополнительно делает:
  5. reparse_alta_inbox --force-dispatch  — переразобрать все CMN/ED.DO1
     (это медленно, ~5-10 мин; обычно не нужно каждые 30 мин)

Защита от двойного запуска: lockfile в LOCK_PATH. Если предыдущий запуск
ещё работает (lock новее N секунд) — выходим без работы. На завершении
lockfile удаляется.

Использование (Task Scheduler):
    # Быстрый (без reparse) — каждые 30 минут
    .\\.venv\\Scripts\\python.exe manage.py auto_sync

    # Полный (с reparse) — раз в N часов
    .\\.venv\\Scripts\\python.exe manage.py auto_sync --full
"""
from __future__ import annotations

import datetime
import os
import sys
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand


LOCK_DIR = os.path.join(os.path.dirname(sys.executable), '..', '..', 'tmp')
LOCK_PATH = os.path.join(os.path.abspath(LOCK_DIR), 'auto_sync.lock')
# Если lock старее этого — считаем что предыдущий запуск умер, перезахватываем.
LOCK_STALE_AFTER_SEC = 30 * 60  # 30 минут


def _acquire_lock() -> bool:
    """Возвращает True если lock захвачен, False если уже занят."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    if os.path.exists(LOCK_PATH):
        age = time.time() - os.path.getmtime(LOCK_PATH)
        if age < LOCK_STALE_AFTER_SEC:
            return False
        # Stale lock — удаляем
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
    help = 'Авто-синхронизация Sheets ↔ БД (для Task Scheduler)'

    def add_arguments(self, parser):
        parser.add_argument('--full', action='store_true',
                            help='+ reparse_alta_inbox --force-dispatch')
        parser.add_argument('--no-lock', action='store_true',
                            help='Игнорировать lockfile')

    def handle(self, *args, **opts):
        started = datetime.datetime.now()
        self.stdout.write(f'[{started.isoformat()}] auto_sync start '
                          f'(full={opts["full"]})')

        if not opts['no_lock']:
            if not _acquire_lock():
                self.stdout.write(self.style.WARNING(
                    'Предыдущий запуск ещё работает (lock занят). Выхожу.'))
                return

        try:
            steps = [
                ('import_sheets', {'source': 'general'}),
                # Импорт CRM-источников (Сводная по СТО, Парсинг (СВОДНАЯ ВЭД)).
                # Нужно для delete_to_client_hawbs — он читает из ImportedSheetRow
                # для tab «Парсинг (СВОДНАЯ ВЭД)». Без этого свежие пометки
                # «ТО КЛИЕНТ» от ВЭД-менеджеров не подхватываются.
                ('import_sheets', {'source': 'crm'}),
                ('relink_hawbs_from_tsd', {'all': True}),
                ('refresh_moscow_cargo', {}),
                # Декларант Плюс (ДВ-склад «Таможенный портал»). No-op если
                # DEKLARANT_ENABLED=False. Гейт внутри команды, ~50 сек при
                # throttle=0.5 и limit=100.
                ('sync_deklarant_svh', {'limit': 100}),
                # «ТО КЛИЕНТ» — физическое удаление строк из CRM-вкладок
                # специалистов когда клиент сам оформляет растаможку.
                # No-op если DELETE_TO_CLIENT_ENABLED=False. Snapshot
                # в backups/to_client_snapshots перед каждым apply.
                ('delete_to_client_hawbs', {'apply': True}),
                # Reconcile статусов СДЭК (safety-net на пропущенные вебхуки).
                # No-op если CDEK_ENABLED=false.
                ('sync_cdek_statuses', {}),
                # sync_filed_dates — выравнивает filed_date по siblings ДТ.
                # Закрывает гонку: CMN.11023 (filed_date) пришёл ДО CMN.11350
                # (ДТ), propagation на siblings не сработал → у одной HAWB
                # filed_date есть, у остальных пусто.
                ('sync_filed_dates', {}),
                ('audit_sheets_vs_db', {'fix': True}),
            ]
            if opts['full']:
                # reparse — между sync и audit, чтобы audit увидел свежие данные
                steps.insert(4, ('reparse_alta_inbox', {'force_dispatch': True}))

            for cmd, kwargs in steps:
                step_started = datetime.datetime.now()
                self.stdout.write('')
                self.stdout.write(self.style.NOTICE(
                    f'[{step_started.isoformat()}] → {cmd}'))
                try:
                    call_command(cmd, **kwargs)
                except Exception as e:
                    self.stdout.write(self.style.ERROR(
                        f'  {cmd} failed: {e}'))
                    # Не падаем — продолжаем остальные шаги.
        finally:
            if not opts['no_lock']:
                _release_lock()

        ended = datetime.datetime.now()
        elapsed = (ended - started).total_seconds()
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'[{ended.isoformat()}] auto_sync done ({elapsed:.0f} sec)'))
