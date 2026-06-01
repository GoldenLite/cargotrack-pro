"""Анализ распределения колонки C по absolute row positions для CRM-вкладки.

Цель: понять почему pass1 sort не утопил blank-C ряды вниз.
Печатает:
  - ws.row_count
  - типы значений в C (int/str/bool/None) и их количества
  - первые и последние позиции каждого типа
  - sample непустых-C рядов после row 500
"""
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand

from cargo.services.sheets.client import get_client


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('tab')

    def handle(self, *args, **opts):
        client = get_client()
        ss = client.open_by_key(CRM_ID)
        ws = next((w for w in ss.worksheets() if w.title == opts['tab']), None)
        if not ws:
            self.stdout.write(f'no tab {opts["tab"]}')
            return

        self.stdout.write(f'Tab: {ws.title}')
        self.stdout.write(f'row_count: {ws.row_count}')
        self.stdout.write(f'col_count: {ws.col_count}')

        # Читаем ТОЛЬКО колонку C через col_values (быстрее)
        col_c = ws.col_values(3, value_render_option='UNFORMATTED_VALUE')
        self.stdout.write(f'col_values(3) len: {len(col_c)}')

        # Распределение типов
        types = Counter()
        positions_by_type = defaultdict(list)
        for i, v in enumerate(col_c[1:], start=2):  # skip header
            t = 'blank' if v in (None, '') else type(v).__name__
            types[t] += 1
            positions_by_type[t].append(i)

        self.stdout.write('\nType distribution in column C:')
        for t, n in types.most_common():
            positions = positions_by_type[t]
            first = positions[0] if positions else '-'
            last = positions[-1] if positions else '-'
            self.stdout.write(f'  {t}: {n} (positions: {first}..{last})')

        # Если есть int+str+bool — sort может быть нестабильным.
        # Покажем последние int/str positions и первые blank positions.
        self.stdout.write('\nDetail by type (first 5, last 5 positions):')
        for t in types:
            positions = positions_by_type[t]
            self.stdout.write(f'  {t}: first={positions[:5]} last={positions[-5:]}')
