"""Диагностика структуры CRM-вкладки.

Печатает шапку и значения первых N рядов (с указанными индексами) для
анализа того что реально лежит в столбце HAWB и других ключевых.

Использование:
    manage.py diag_crm_tab "Беляева Екатерина" --rows 544,545,548,558,559,562,563
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.services.sheets.client import get_client


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'


def _col_letter(idx: int) -> str:
    s = ''
    n = idx
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('tab')
        parser.add_argument('--rows', default='1,2,3,4,5',
                            help='CSV row indices (1-based) для дампа')
        parser.add_argument('--cols', type=int, default=25,
                            help='Сколько колонок дампать')

    def handle(self, *args, **opts):
        client = get_client()
        ss = client.open_by_key(CRM_ID)
        ws = None
        for w in ss.worksheets():
            if w.title == opts['tab']:
                ws = w
                break
        if not ws:
            self.stdout.write(f'Tab not found: {opts["tab"]}')
            return

        n_cols = opts['cols']
        last_col = _col_letter(n_cols)
        rng = f'A1:{last_col}{ws.row_count}'
        self.stdout.write(f'Reading {rng} ({ws.title}, {ws.row_count}×{ws.col_count})')
        all_vals = ws.get(rng, value_render_option='UNFORMATTED_VALUE')

        # Шапка
        if all_vals:
            self.stdout.write('\nHeader (row 1):')
            for i, h in enumerate(all_vals[0], start=1):
                if h:
                    self.stdout.write(f'  {_col_letter(i)}: {h!r}')

        rows = [int(s.strip()) for s in opts['rows'].split(',') if s.strip()]
        for r in rows:
            self.stdout.write(f'\nRow {r}:')
            if r - 1 >= len(all_vals):
                self.stdout.write('  (no data — row beyond data range)')
                continue
            row = all_vals[r - 1]
            for i, v in enumerate(row, start=1):
                if v is None or v == '':
                    continue
                self.stdout.write(
                    f'  {_col_letter(i)}: type={type(v).__name__}  '
                    f'val={v!r}')
