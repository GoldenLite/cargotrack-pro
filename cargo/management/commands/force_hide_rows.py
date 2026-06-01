"""Force-hide указанных рядов в CRM-вкладке."""
from django.core.management.base import BaseCommand

from cargo.services.sheets.client import get_client


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('tab')
        parser.add_argument('rows', nargs='+', type=int)
        parser.add_argument('--show', action='store_true')

    def handle(self, *args, **opts):
        client = get_client()
        ss = client.open_by_key(CRM_ID)
        ws = next((w for w in ss.worksheets() if w.title == opts['tab']), None)
        if not ws:
            self.stdout.write(f'no tab')
            return

        hidden = not opts['show']
        requests = []
        for r in opts['rows']:
            requests.append({
                'updateDimensionProperties': {
                    'range': {
                        'sheetId': ws.id,
                        'dimension': 'ROWS',
                        'startIndex': r - 1,
                        'endIndex': r,
                    },
                    'properties': {'hiddenByUser': hidden},
                    'fields': 'hiddenByUser',
                }
            })
        result = ss.batch_update({'requests': requests})
        self.stdout.write(f'Done: set hidden={hidden} for {len(requests)} rows')
        self.stdout.write(f'Response: {result}')
