"""Принудительно запускает pass1 sort на вкладке и печатает before/after."""
from collections import Counter

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

        self.stdout.write(f'Tab: {ws.title}, row_count={ws.row_count}')

        # Before
        before = ws.col_values(3, value_render_option='UNFORMATTED_VALUE')[1:]
        c = Counter('blank' if v in (None, '') else type(v).__name__ for v in before)
        first_blank = next((i for i, v in enumerate(before, start=2) if v in (None, '')), None)
        last_nonblank = next((i for i, v in reversed(list(enumerate(before, start=2))) if v not in (None, '')), None)
        self.stdout.write(f'BEFORE: {dict(c)}, first_blank={first_blank}, last_nonblank={last_nonblank}')

        # Запускаем pass 1
        req = {
            'sortRange': {
                'range': {
                    'sheetId': ws.id,
                    'startRowIndex': 1,
                    'endRowIndex': ws.row_count,
                    'startColumnIndex': 0,
                    'endColumnIndex': 24,
                },
                'sortSpecs': [
                    {'dimensionIndex': 2, 'sortOrder': 'ASCENDING'},
                ],
            }
        }
        self.stdout.write('Issuing pass1 sort...')
        result = ss.batch_update({'requests': [req]})
        self.stdout.write(f'Response keys: {list(result.keys()) if isinstance(result, dict) else result}')

        # After
        after = ws.col_values(3, value_render_option='UNFORMATTED_VALUE')[1:]
        c2 = Counter('blank' if v in (None, '') else type(v).__name__ for v in after)
        first_blank_a = next((i for i, v in enumerate(after, start=2) if v in (None, '')), None)
        last_nonblank_a = next((i for i, v in reversed(list(enumerate(after, start=2))) if v not in (None, '')), None)
        self.stdout.write(f'AFTER:  {dict(c2)}, first_blank={first_blank_a}, last_nonblank={last_nonblank_a}')

        # Sample positions
        self.stdout.write('\nFirst 20 cells after sort (column C):')
        for i, v in enumerate(after[:20], start=2):
            self.stdout.write(f'  row {i}: {v!r}')
        self.stdout.write('\nCells 540-565 after sort:')
        for i in range(540, 566):
            if i - 2 < len(after):
                self.stdout.write(f'  row {i}: {after[i-2]!r}')
