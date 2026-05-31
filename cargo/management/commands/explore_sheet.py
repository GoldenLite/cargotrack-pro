"""Изучить структуру Google Sheet — все вкладки, шапки, пара примеров строк."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.services.sheets.client import get_client


class Command(BaseCommand):
    help = 'Дамп структуры Google Sheet (все вкладки + шапки + примеры).'

    def add_arguments(self, parser):
        parser.add_argument('spreadsheet_id')
        parser.add_argument('--rows', type=int, default=3)

    def handle(self, *args, **opts):
        client = get_client()
        try:
            ss = client.open_by_key(opts['spreadsheet_id'])
        except Exception as e:
            self.stdout.write(f'Не открыть таблицу: {e}')
            return
        self.stdout.write(f'Spreadsheet: {ss.title}')
        for ws in ss.worksheets():
            self.stdout.write('')
            self.stdout.write(self.style.NOTICE(
                f'=== Вкладка: {ws.title}  (gid={ws.id}, '
                f'{ws.row_count}x{ws.col_count}) ==='))
            try:
                header = ws.row_values(1)
                self.stdout.write(f'  Колонок в шапке: {len(header)}')
                for i, h in enumerate(header, start=1):
                    if (h or '').strip():
                        self.stdout.write(f'    {i:3d}. {h!r}')
                if opts['rows']:
                    self.stdout.write('  --- примеры строк ---')
                    for r in range(2, 2 + opts['rows']):
                        vals = ws.row_values(r)
                        if any((v or '').strip() for v in vals):
                            self.stdout.write(
                                f'    row {r}: '
                                + ' | '.join(
                                    f'{header[i]}={v!r}'
                                    for i, v in enumerate(vals[:8])
                                    if (v or '').strip()))
            except Exception as e:
                self.stdout.write(f'  ошибка: {e}')
