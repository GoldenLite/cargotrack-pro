"""Проверка реального hidden state ряда в Google Sheets через API."""
from django.core.management.base import BaseCommand

from cargo.services.sheets.client import get_client


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('tab')
        parser.add_argument('rows', nargs='+', type=int)

    def handle(self, *args, **opts):
        client = get_client()
        ss = client.open_by_key(CRM_ID)
        ws = next((w for w in ss.worksheets() if w.title == opts['tab']), None)
        if not ws:
            self.stdout.write(f'no tab {opts["tab"]}')
            return

        # Запрашиваем sheets.get с rowMetadata.hiddenByUser
        rng = ws.title
        meta = ss.fetch_sheet_metadata(
            params={
                'ranges': rng,
                'fields': 'sheets(properties(sheetId,title),data(rowMetadata(hiddenByUser)))',
            },
        )
        for sh in meta['sheets']:
            if sh['properties']['title'] != opts['tab']:
                continue
            data = sh.get('data', [])
            if not data:
                self.stdout.write('no data')
                return
            row_metas = data[0].get('rowMetadata', [])
            for r in opts['rows']:
                idx = r - 1  # 0-based
                if idx >= len(row_metas):
                    self.stdout.write(f'  row {r}: beyond range')
                    continue
                rm = row_metas[idx]
                self.stdout.write(
                    f'  row {r}: hidden={rm.get("hiddenByUser", False)}')
