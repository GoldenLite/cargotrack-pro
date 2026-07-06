"""Перепривязывает HAWB → Cargo по ТСД из Sheets «Общее».

В Sheets юзер вписывает MAWB партии в колонку «ТСД» (формат 784-82334582)
рядом с HAWB. promote_row уже умеет это использовать — но только при
СОЗДАНИИ HAWB. Если HAWB уже создан раньше с другим/без ТСД, а юзер потом
уточнил ТСД в Sheets — наш код это игнорирует.

Эта команда:
1. Берёт ImportedSheetRow из «Общее» (по списку HAWB или все).
2. Для каждой читает data['ТСД'].
3. Если соответствующий HAWB в БД привязан к другой Cargo (или не привязан)
   → перепривязывает к Cargo с awb_number=ТСД (создаёт DRAFT если нет).
4. Очищает старые HAWB-поля от ЧУЖОГО ED.DO1 (svh_do1_sent_at, weight,
   places, svh_do2_send_at) — они принципиально неактуальны при смене партии.

После завершения — guard в outbox._link_hawbs_to_cargo не даст ED.DO1
снова перетянуть HAWB в чужую партию.

Дальнейшая цепочка (после этой команды):
  refresh_moscow_cargo                       # подтянуть MC-данные
  reparse_alta_inbox --force-dispatch        # применить наши CMN
  redispatch_alta_outbox --type ED.DO1       # применить наши ED.DO1

Запуск:
    # По списку (рекомендуется):
    uv run python manage.py relink_hawbs_from_tsd --file hawbs.txt
    # Все HAWB в Sheets «Общее»:
    uv run python manage.py relink_hawbs_from_tsd --all
    # Dry-run для проверки:
    uv run python manage.py relink_hawbs_from_tsd --file hawbs.txt --dry-run
"""
from __future__ import annotations

import os
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cargo.models import Cargo, HouseWaybill, ImportedSheetRow
from cargo.services.sheets.mapping import GEN_TSD
from cargo.services.sheets.transport import guess_transport_mode


class Command(BaseCommand):
    help = 'Перепривязать HAWB к Cargo по ТСД из Sheets «Общее»'

    def add_arguments(self, parser):
        parser.add_argument('--file', default='',
                            help='Файл со списком HAWB (по одному на строку)')
        parser.add_argument('--all', action='store_true',
                            help='Все ImportedSheetRow из «Общее» с непустым ТСД')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--no-clear-svh', action='store_true',
                            help='Не очищать СВХ-поля HAWB при перепривязке')

    def handle(self, *args, **opts):
        if not opts['file'] and not opts['all']:
            raise CommandError('Нужен --file или --all')

        # Список HAWB для фильтра
        target_hawbs: set = set()
        if opts['file']:
            if not os.path.exists(opts['file']):
                raise CommandError(f'Файл не найден: {opts["file"]}')
            with open(opts['file'], 'r', encoding='utf-8-sig') as f:
                target_hawbs = {
                    s.strip() for s in f
                    if s.strip() and not s.startswith('#')
                }
            self.stdout.write(f'HAWB в списке: {len(target_hawbs)}')

        # ImportedSheetRow с непустым ТСД
        rows_qs = ImportedSheetRow.objects.filter(
            source__kind='general',
        ).exclude(hawb_number_norm='')
        if target_hawbs:
            rows_qs = rows_qs.filter(hawb_number_norm__in=target_hawbs)

        # Дедуп по hawb_number_norm (берём самую свежую)
        seen = set()
        rows = []
        for r in rows_qs.order_by('-last_imported_at'):
            if r.hawb_number_norm in seen:
                continue
            seen.add(r.hawb_number_norm)
            rows.append(r)
        self.stdout.write(f'ImportedSheetRow найдено: {len(rows)}')

        # Анализ: что в data['ТСД']
        stats = Counter()
        actions = []  # [(hawb, tsd, current_mawb_str)]
        targets_to_create: set = set()

        # Pre-load all HAWB
        hawbs_db = {
            h.hawb_number: h for h in HouseWaybill.objects.filter(
                hawb_number__in=[r.hawb_number_norm for r in rows]
            ).select_related('mawb')
        }
        # Pre-load Cargos для всех непустых ТСД
        tsd_set = {(r.data or {}).get(GEN_TSD, '').strip()
                   for r in rows}
        tsd_set = {t for t in tsd_set if t}
        cargos_db = {
            c.awb_number: c for c in Cargo.objects.filter(awb_number__in=tsd_set)
        }

        for r in rows:
            tsd = ((r.data or {}).get(GEN_TSD) or '').strip()
            hn = r.hawb_number_norm
            h = hawbs_db.get(hn)
            if not h:
                stats['hawb_not_in_db'] += 1
                continue
            if not tsd:
                stats['no_tsd_in_sheets'] += 1
                continue
            current_mawb = h.mawb.awb_number if h.mawb_id and h.mawb else ''
            if current_mawb == tsd:
                stats['already_correct'] += 1
                continue
            actions.append((h, tsd, current_mawb))
            if tsd not in cargos_db:
                targets_to_create.add(tsd)

        # Сводка по target MAWB
        target_counter = Counter(tsd for _, tsd, _ in actions)
        self.stdout.write('')
        self.stdout.write('План:')
        self.stdout.write(f'  Требуется перепривязка:           {len(actions)}')
        self.stdout.write(f'  HAWB не в БД:                     {stats["hawb_not_in_db"]}')
        self.stdout.write(f'  ТСД пустой в Sheets:              {stats["no_tsd_in_sheets"]}')
        self.stdout.write(f'  Уже привязан правильно:           {stats["already_correct"]}')
        self.stdout.write(f'  Cargo создать (новые ТСД):        {len(targets_to_create)}')
        if target_counter:
            self.stdout.write('  Целевые партии (top 10):')
            for tsd, n in target_counter.most_common(10):
                marker = '+CREATE' if tsd in targets_to_create else ''
                self.stdout.write(f'    {tsd}: {n} HAWB {marker}')
            if len(target_counter) > 10:
                self.stdout.write(f'    ... ещё {len(target_counter)-10}')

        if opts['dry_run']:
            self.stdout.write('')
            self.stdout.write('--- DRY RUN, первые 15: ---')
            for h, tsd, cur in actions[:15]:
                self.stdout.write(
                    f'  {h.hawb_number}: {cur or "(нет)"} → {tsd}'
                )
            return

        if not actions:
            self.stdout.write(self.style.SUCCESS('Нечего перепривязывать.'))
            return

        # Создать недостающие Cargo
        for tsd in sorted(targets_to_create):
            c = Cargo.objects.create(
                awb_number=tsd,
                transportation_mode=guess_transport_mode(tsd),
                stage='DRAFT',
                is_draft=True,
            )
            cargos_db[tsd] = c
            self.stdout.write(f'  Created Cargo {tsd} (pk={c.pk})')

        # Перепривязка (прямой UPDATE минуя save).
        # Чанкуем по 200 — каждый чанк отдельная короткая транзакция, чтобы
        # НЕ держать SQLite write-lock на весь цикл (при большом actions это
        # минуты → database is locked у параллельных писателей agent/cron).
        # Идемпотентно: частичный прогон безопасен, следующий запуск дочинит
        # остаток (actions пересчитывается из текущего state).
        clear_svh = not opts['no_clear_svh']
        moved = 0
        RELINK_CHUNK = 200
        for _ci in range(0, len(actions), RELINK_CHUNK):
            with transaction.atomic():
                for h, tsd, _ in actions[_ci:_ci + RELINK_CHUNK]:
                    cargo = cargos_db.get(tsd)
                    if not cargo:
                        continue
                    update_fields = {'mawb_id': cargo.pk}
                    if clear_svh:
                        update_fields.update({
                            'svh_do1_sent_at': None,
                            'svh_do1_gross_weight': None,
                            'svh_do1_place_count': None,
                            'svh_do2_send_at': None,
                        })
                    HouseWaybill.objects.filter(pk=h.pk).update(**update_fields)
                    moved += 1
        self.stdout.write(self.style.SUCCESS(
            f'Перепривязано: {moved} HAWB'))

        # Подсказка
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE(
            'Дальше:\n'
            '  manage.py refresh_moscow_cargo                # MC-данные\n'
            '  manage.py reparse_alta_inbox --force-dispatch  # наши CMN\n'
            '  manage.py redispatch_alta_outbox --type ED.DO1 # наши ED.DO1\n'
        ))
