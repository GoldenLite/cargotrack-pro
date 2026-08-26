"""Само-лечение колонки «Регистрационный номер ДТ» (X, GEN_DECLARATION) в «Общее».

Колонку X пишет `_fill_empty_user_decl` ТОЛЬКО в момент выпуска (внутри
batch_write_declarations_for_hawbs). Аудит её НЕ покрывает (в отличие от
ed_status/Z), поэтому промах при выпуске (нет ISR в тот момент / стоял конвейер /
HAWB не попала в батч) остаётся ПУСТЫМ навсегда. Эта команда — недостающая
страховка: прогоняет `_fill_empty_user_decl` по выпущенным ИМПОРТ-накладным.

Правило fill-empty сохраняется (ручной ввод не перетираем, чужие значения не
трогаем) — берём ту же функцию, что и на выпуске.

    manage.py heal_user_decl              # окно последних --days (по умолч. 21)
    manage.py heal_user_decl --all        # ВСЕ выпущенные (разовый бэкфилл)
    manage.py heal_user_decl --dry-run
"""
from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Само-лечение колонки «Регистрационный номер ДТ» (X) для выпущенных.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=21,
                            help='Окно по release_date (по умолчанию 21 день). '
                                 'Игнорируется при --all.')
        parser.add_argument('--all', action='store_true',
                            help='Все выпущенные (для разового бэкфилла).')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        from cargo.models import HouseWaybill
        from cargo.services.alta.ed_status import ed_status_batch
        from cargo.services.sheets.writeback import (
            _fill_empty_user_decl, _kind_for_hawb)

        qs = (HouseWaybill.objects
              .filter(customs_status='RELEASED')
              .exclude(customs_declaration_number='')
              .select_related('mawb')
              .prefetch_related('declaration_attempts'))
        if not opts['all']:
            cutoff = timezone.now() - datetime.timedelta(days=opts['days'])
            qs = qs.filter(release_date__gte=cutoff)
        hawbs = [h for h in qs if _kind_for_hawb(h) != 'export']
        if opts['limit']:
            hawbs = hawbs[:opts['limit']]
        self.stdout.write(f'выпущенных ИМПОРТ с decl в наборе: {len(hawbs)}')
        if not hawbs:
            return

        if opts['dry_run']:
            self.stdout.write('(dry-run — без записи; реальный прогон заполнит '
                              'пустые ячейки X)')
            return

        with ed_status_batch():
            n = _fill_empty_user_decl(hawbs)
        self.stdout.write(self.style.SUCCESS(
            f'heal_user_decl: записано ячеек X = {n}'))
