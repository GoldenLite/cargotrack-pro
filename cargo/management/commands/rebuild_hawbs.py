"""Полная перестройка автоматических данных по списку HAWB.

Используется когда видно несоответствия в БД (например HAWB привязан к
чужому Cargo, стейл-ДТ, не той лицензии СВХ) и хочется чистый restart:
обнулить всё что приходит из автоматических источников и собрать заново
из CMN/ED.DO1/moscow-cargo/Sheets ТСД.

Что ОБНУЛЯЕТ в БД (только автоматические поля):
  HouseWaybill:
    mawb_id            → None (связь с Cargo, восстановится по ТСД)
    customs_status     → ''
    customs_declaration_number → ''
    filed_date         → None
    release_date       → None
    svh_do1_sent_at    → None
    svh_do1_gross_weight → None
    svh_do1_place_count → None
    svh_do2_send_at    → None
    logistics_status   → 'AT_ORIGIN_WH' (если был post-customs, иначе оставляем)

НЕ ТРОГАЕТ (ручные / из Sheets-импорта поля):
  hawb_number, weight, places, tsd_number, notes,
  cargo_type, ved_manager, problem_note, assigned_to, scan_into_bond
  (последнее — у Cargo, мы Cargo не трогаем).

Что ОЧИЩАЕТ в Sheets: все 11 CargoTrack-колонок для этих HAWB.
Что ВЫЗЫВАЕТ: relink_hawbs_from_tsd для этих HAWB (привязка по ТСД).

Запуск:
    uv run python manage.py rebuild_hawbs --file hawbs.txt
    uv run python manage.py rebuild_hawbs --file hawbs.txt --dry-run

После — отдельно:
    manage.py refresh_moscow_cargo                # MC-данные
    manage.py reparse_alta_inbox --force-dispatch # все CMN/ED.DO1
    manage.py audit_sheets_vs_db --fix            # финальная сверка Sheets
"""
from __future__ import annotations

import os

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cargo.models import HouseWaybill


POST_CUSTOMS_STATES = {
    'READY_DELIVERY', 'IN_TRANSIT_DEST', 'DELIVERED',
    'IN_TRANSIT_EXP',
}


class Command(BaseCommand):
    help = 'Обнулить автоматические поля HAWB и перепривязать по ТСД'

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True,
                            help='Файл со списком HAWB (по одному на строку)')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--no-clear-sheets', action='store_true',
                            help='Не очищать Sheets-ячейки (только БД)')
        parser.add_argument('--no-relink', action='store_true',
                            help='Не делать relink_hawbs_from_tsd после')

    def handle(self, *args, **opts):
        path = opts['file']
        if not os.path.exists(path):
            raise CommandError(f'Файл не найден: {path}')

        with open(path, 'r', encoding='utf-8-sig') as f:
            hawbs = [s.strip() for s in f if s.strip() and not s.startswith('#')]
        hawbs = list(dict.fromkeys(hawbs))  # дедуп, сохраняя порядок
        self.stdout.write(f'HAWB в списке: {len(hawbs)}')

        in_db = list(HouseWaybill.objects.filter(hawb_number__in=hawbs))
        self.stdout.write(f'Из них в БД: {len(in_db)}')

        if opts['dry_run']:
            self.stdout.write('--- DRY RUN — первые 10 ---')
            for h in in_db[:10]:
                cur_mawb = h.mawb.awb_number if h.mawb_id and h.mawb else '(нет)'
                self.stdout.write(
                    f'  {h.hawb_number}: mawb={cur_mawb} '
                    f'status={h.customs_status!r} '
                    f'ДТ={h.customs_declaration_number!r} '
                    f'do1={h.svh_do1_sent_at} do2={h.svh_do2_send_at}'
                )
            self.stdout.write(f'  ... всего обработается {len(in_db)}')
            return

        if not in_db:
            self.stdout.write('Нечего обнулять.')
            return

        # ── Шаг 1. Обнулить автоматические поля в БД ──
        # Прямой UPDATE минуя save() — обходим валидации (mawb→AT_ORIGIN_WH
        # с auto-cleanup и т.п.) и автосбросы Rule 0.
        pks = [h.pk for h in in_db]

        # Для HAWB которые в post-customs (RELEASED → READY_DELIVERY) надо
        # logistics_status вернуть к AT_ORIGIN_WH (там Rule 0 ВЕТО ставит).
        post_pks = [
            h.pk for h in in_db
            if h.logistics_status in POST_CUSTOMS_STATES
        ]

        with transaction.atomic():
            HouseWaybill.objects.filter(pk__in=pks).update(
                mawb_id=None,
                customs_status='',
                customs_declaration_number='',
                filed_date=None,
                release_date=None,
                svh_do1_sent_at=None,
                svh_do1_gross_weight=None,
                svh_do1_place_count=None,
                svh_do2_send_at=None,
            )
            if post_pks:
                HouseWaybill.objects.filter(pk__in=post_pks).update(
                    logistics_status='AT_ORIGIN_WH',
                )
        self.stdout.write(self.style.SUCCESS(
            f'Обнулено в БД: {len(pks)} HAWB '
            f'(logistics_status откатан у {len(post_pks)})'
        ))

        # ── Шаг 2. Очистить Sheets-ячейки ──
        if not opts['no_clear_sheets']:
            self.stdout.write('')
            self.stdout.write(self.style.NOTICE('Очистка Sheets...'))
            try:
                call_command('clear_cargotrack_for_hawbs', file=path)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'clear failed: {e}'))

        # ── Шаг 3. Перепривязать по ТСД ──
        if not opts['no_relink']:
            self.stdout.write('')
            self.stdout.write(self.style.NOTICE('Перепривязка по ТСД...'))
            try:
                call_command('relink_hawbs_from_tsd', file=path)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'relink failed: {e}'))

        # ── Подсказка ──
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE(
            'Дальше — собрать данные обратно:\n'
            '  manage.py refresh_moscow_cargo\n'
            '  manage.py reparse_alta_inbox --force-dispatch\n'
            '  manage.py redispatch_alta_outbox --type ED.DO1\n'
            '  manage.py audit_sheets_vs_db --fix\n'
        ))
