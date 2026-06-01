"""Ищет указанные HAWB во ВСЕХ CRM-вкладках + проверяет hidden state."""
from django.core.management.base import BaseCommand

from cargo.services.sheets.client import get_client


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'

SPECIALIST_TABS = {
    'Беляева Екатерина',
    'Калина Елена',
    'Коробкова Екатерина',
    'Азамов Азам',
    'Никонова Светлана',
    'Подолин Алексей',
    'Пругар Ольга',
    'Алексеева Екатерина',
    'Шушарина Татьяна',
}

COL_HAWB      = 3
COL_DECL      = 23
COL_ED_STATUS = 24


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='+')

    def handle(self, *args, **opts):
        targets = set(opts['hawbs'])
        client = get_client()
        ss = client.open_by_key(CRM_ID)

        # Один запрос metadata по всем вкладкам — получаем hiddenByUser для всех рядов
        all_tabs = [w for w in ss.worksheets() if w.title in SPECIALIST_TABS]
        ranges_param = ','.join(w.title for w in all_tabs)
        meta = ss.fetch_sheet_metadata(
            params={
                'ranges': ranges_param,
                'fields': 'sheets(properties(sheetId,title),data(rowMetadata(hiddenByUser)))',
            },
        )
        hidden_by_tab = {}  # tab_name -> list[bool]
        for sh in meta['sheets']:
            t = sh['properties']['title']
            data = sh.get('data', [])
            if data:
                hidden_by_tab[t] = [
                    rm.get('hiddenByUser', False)
                    for rm in data[0].get('rowMetadata', [])
                ]

        # Сканируем колонки HAWB+decl+ed_status в каждой вкладке
        for ws in all_tabs:
            vals = ws.get(f'A1:{chr(ord("A") + COL_ED_STATUS - 1)}{ws.row_count}',
                          value_render_option='UNFORMATTED_VALUE')
            hidden_arr = hidden_by_tab.get(ws.title, [])
            for i, row in enumerate(vals[1:], start=2):
                if COL_HAWB - 1 >= len(row):
                    continue
                hn = str(row[COL_HAWB - 1]).strip()
                if hn not in targets:
                    continue
                decl = (str(row[COL_DECL - 1]).strip()
                        if COL_DECL - 1 < len(row) else '')
                ed = (str(row[COL_ED_STATUS - 1]).strip()
                      if COL_ED_STATUS - 1 < len(row) else '')
                is_hidden = hidden_arr[i - 1] if i - 1 < len(hidden_arr) else False
                self.stdout.write(
                    f'  {ws.title} row={i} hawb={hn} hidden={is_hidden} '
                    f'decl={decl!r} ed={ed!r}')
