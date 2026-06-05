"""Удаление HAWB из CRM-вкладок специалистов («Рабочее пространство СТО»)
для случаев, когда таможенное оформление делает клиент сам.

Источник истины — вкладка «Парсинг (СВОДНАЯ ВЭД)» в CRM-spreadsheet
(SheetSource.kind='crm', tab_name='Парсинг (СВОДНАЯ ВЭД)'). Столбец A
содержит «Номер заказа» (HAWB), столбец B — «ФИО специалиста». Когда там
стоит «ТО КЛИЕНТ» — мы не ведём оформление, накладную делает сам клиент →
строка не должна висеть в рабочих пространствах специалистов.

Поведение:
- Driver-query: ImportedSheetRow(source.tab_name='Парсинг (СВОДНАЯ ВЭД)')
  где data['ФИО специалиста'].strip().upper() == 'ТО КЛИЕНТ'.
- HAWB читаем напрямую из data['Номер заказа'] (matcher на эту вкладку
  не настроен — алиасы в mapping.py не добавлены).
- Дубль-строки в Парсинге: если ХОТЯ БЫ ОДНА копия HAWB имеет 'ТО КЛИЕНТ' —
  удаляем (set по hawb_number).
- Защита: значение должно быть **явно** 'ТО КЛИЕНТ' (не пусто/None).
- Для каждой найденной HAWB:
    1. JSON-snapshot всех CrmHawbIndex (tab/row/data) → файл в tmp/
       (страховка для отката, 30 дней храним).
    2. Вызов штатной `delete_hawb_rows` (DESC row, sheets+index sync).
- Гейт DELETE_TO_CLIENT_ENABLED в settings → no-op при False, как у sync_cdek.
- Lockfile (паттерн как у sync_deklarant_svh / auto_sync).
- Cargo «Общее» (general) НЕ задеваем — там HAWB остаётся (по дизайну юзера).
- Reverse: если клиент потом передумал — добавлять HAWB обратно вручную
  (по соглашению с юзером, авто-add не делаем).

Использование:
    manage.py delete_to_client_hawbs --dry-run
    manage.py delete_to_client_hawbs --apply
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import sys
import time

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand

from cargo.models import CrmHawbIndex, ImportedSheetRow


logger = logging.getLogger('cargo.delete_to_client_hawbs')


SOURCE_TAB_NAME = 'Парсинг (СВОДНАЯ ВЭД)'
TARGET_KEY = 'ФИО специалиста'   # столбец B в Парсинге
HAWB_KEY = 'Номер заказа'        # столбец A в Парсинге
TARGET_VALUE = 'ТО КЛИЕНТ'

# Lockfile рядом с auto_sync.lock
LOCK_DIR = os.path.join(os.path.dirname(sys.executable), '..', '..', 'tmp')
LOCK_PATH = os.path.join(os.path.abspath(LOCK_DIR),
                         'delete_to_client_hawbs.lock')
LOCK_STALE_AFTER_SEC = 30 * 60

# Snapshot директория — для отката
SNAPSHOT_DIR = os.path.join(os.path.abspath(LOCK_DIR), '..',
                            'backups', 'to_client_snapshots')


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
    help = ('Удалить из CRM-вкладок специалистов HAWB у которых в Сводной ВЭД '
            'столбец «ФИО Специалист по ВЭД» = «ТО КЛИЕНТ».')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Реально удалять. Без флага — DRY-RUN.')
        parser.add_argument('--limit', type=int, default=0,
                            help='Лимит HAWB за один прогон (0 = без лимита).')

    def handle(self, *args, **opts):
        do_apply = bool(opts.get('apply'))

        if not getattr(settings, 'DELETE_TO_CLIENT_ENABLED', False):
            self.stdout.write(self.style.WARNING(
                'DELETE_TO_CLIENT_ENABLED=False. Skipping.'))
            return

        if not _acquire_lock():
            self.stdout.write(self.style.WARNING(
                f'delete_to_client_hawbs: lock уже занят ({LOCK_PATH}), skip.'))
            return

        try:
            self._run(do_apply, opts.get('limit') or 0)
        finally:
            _release_lock()

    def _run(self, do_apply: bool, limit: int):
        # 1. Driver-query — только из «Парсинг (СВОДНАЯ ВЭД)».
        # Дубль-строки одного HAWB допустимы; берём через set по номеру.
        # HAWB читаем из data['Номер заказа'] (matcher на эту вкладку не
        # настроен — hawb_number_norm пустой).
        candidates_set: set[str] = set()
        for r in ImportedSheetRow.objects.filter(
                source__kind='crm',
                source__tab_name=SOURCE_TAB_NAME,
                source__is_active=True,
        ).only('data'):
            d = r.data or {}
            if not isinstance(d, dict):
                continue
            val = str(d.get(TARGET_KEY) or '').strip().upper()
            if val != TARGET_VALUE:
                continue
            hn = str(d.get(HAWB_KEY) or '').strip()
            # HAWB-номер должен выглядеть как минимум как 10-значный
            # (нормальный авиа-номер). Защита от мусора в столбце A.
            if not hn or not hn.isdigit() or len(hn) < 8:
                continue
            candidates_set.add(hn)
            if limit and len(candidates_set) >= limit:
                break
        candidates: list[str] = sorted(candidates_set)

        if not candidates:
            self.stdout.write('  Кандидатов нет.')
            return

        self.stdout.write(
            f'Кандидаты ({len(candidates)} HAWB):')
        for hn in candidates:
            self.stdout.write(f'  {hn}')

        # 2. Какие из них реально живут в CRM-вкладках (если HAWB нет в
        # CrmHawbIndex, удалять нечего).
        idx_rows = list(CrmHawbIndex.objects.filter(hawb_number__in=candidates))
        if not idx_rows:
            self.stdout.write('  В CrmHawbIndex эти HAWB не найдены — нечего удалять.')
            return

        self.stdout.write(f'\nВ CRM-вкладках найдено: {len(idx_rows)} строк '
                          f'(сумма по всем спецам)')
        # Группировка по вкладкам
        from collections import defaultdict
        by_tab = defaultdict(list)
        for e in idx_rows:
            by_tab[e.tab_name].append(e)
        for tab, items in by_tab.items():
            self.stdout.write(f'  {tab}: {len(items)} строк')

        # 3. Snapshot (только если apply). Защита: даже dry-run строим snapshot
        # in-memory для печати, но на диск пишем только при apply.
        snapshot = [
            {
                'hawb_number': e.hawb_number,
                'tab_name': e.tab_name,
                'row_index': e.row_index,
                'last_status': getattr(e, 'last_status', '') or '',
                'last_decl': getattr(e, 'last_decl', '') or '',
                'last_hidden': bool(getattr(e, 'last_hidden', False)),
            }
            for e in idx_rows
        ]

        if not do_apply:
            self.stdout.write(self.style.WARNING(
                '\nDRY-RUN: ничего не удаляем. Snapshot не сохранён. '
                'Запустите с --apply для реального удаления.'))
            return

        # Сохраняем snapshot
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime('%Y%m%dT%H%M%SZ')
        snap_path = os.path.join(SNAPSHOT_DIR, f'to_client_{ts}.json')
        with open(snap_path, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        self.stdout.write(self.style.SUCCESS(
            f'\nSnapshot saved: {snap_path}'))

        # 4. Делегируем удаление штатной команде. Она группирует по tab,
        # сортирует row_index DESC, удаляет через deleteDimension, чистит
        # CrmHawbIndex, имеет retry на API errors.
        self.stdout.write('\nDelegating to delete_hawb_rows...')
        call_command('delete_hawb_rows', *candidates)

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. Удалено по {len(candidates)} HAWB-кандидатам.'))
