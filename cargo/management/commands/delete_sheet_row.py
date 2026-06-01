"""Удалить ряд в CRM-вкладке через deleteDimension."""
from django.core.management.base import BaseCommand

from cargo.services.sheets.client import get_client


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('tab')
        parser.add_argument('rows', nargs='+', type=int,
                            help='Row indices (1-based) для удаления')

    def handle(self, *args, **opts):
        client = get_client()
        ss = client.open_by_key(CRM_ID)
        ws = next((w for w in ss.worksheets() if w.title == opts['tab']), None)
        if not ws:
            self.stdout.write(f'no tab {opts["tab"]}')
            return

        # Сортируем по убыванию чтобы удаление не сдвинуло
        # последующие row_index'ы.
        rows_desc = sorted(set(opts['rows']), reverse=True)
        requests = []
        for r in rows_desc:
            requests.append({
                'deleteDimension': {
                    'range': {
                        'sheetId': ws.id,
                        'dimension': 'ROWS',
                        'startIndex': r - 1,
                        'endIndex': r,
                    }
                }
            })
        result = ss.batch_update({'requests': requests})
        self.stdout.write(f'Deleted rows {rows_desc} in {opts["tab"]}')
        self.stdout.write(f'Response: {len(result.get("replies", []))} replies')
