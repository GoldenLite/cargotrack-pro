"""Аудит CRM-вкладок: ищем «осиротевшие» скрытые ряды — те, что в Sheets
скрыты, но по нашим правилам hide-логики скрываться НЕ должны (нет
выпуска, нет legacy decl-без-статуса). Обычно остаются после старого
reindex когда decl стёрли (rejected) но hidden флаг не пересмотрели.

С `--apply` — раскрывает в Sheets + обновляет CrmHawbIndex.last_hidden=False.
"""
import logging
import time
from collections import defaultdict

from django.core.management.base import BaseCommand
import gspread.exceptions

from cargo.models import CrmHawbIndex, HouseWaybill
from cargo.services.alta.ed_status import compute_ed_status
from cargo.services.sheets.client import get_client


logger = logging.getLogger('cargo.audit_orphan_hidden')


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'

from cargo.services.sheets.crm_tabs import SPECIALIST_TABS  # noqa: E402  единый whitelist вкладок


def _retry(fn, *args, label='', **kwargs):
    backoff = [1, 2, 4, 8, 16, 32]
    for attempt in range(len(backoff) + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(e.response, 'status_code', None)
            if status in (429, 500, 502, 503, 504) and attempt < len(backoff):
                time.sleep(backoff[attempt])
                continue
            raise


def _compute_want_hidden(entry: CrmHawbIndex, h: HouseWaybill | None) -> bool:
    """Та же логика что в crm_sync_incremental: hide только при «Выпуск
    разрешен» или legacy «decl-без-статуса»."""
    if h:
        new_decl = (h.customs_declaration_number or '').strip()
        new_status = compute_ed_status(h) or ''
        if 'Выпуск разрешен' in new_status:
            will_decl = new_decl
        elif any(m in new_status for m in
                 ('Отказ', 'Отзыв', 'Считается не поданной')):
            will_decl = ''
        elif not new_decl:
            will_decl = ''
        else:
            will_decl = entry.last_decl
        will_status = new_status
    else:
        will_decl = entry.last_decl
        will_status = entry.last_status

    is_legacy_released = bool(will_decl) and not will_status
    return ('Выпуск разрешен' in will_status) or is_legacy_released


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--tab', help='Только эта вкладка')

    def handle(self, *args, **opts):
        client = get_client()
        ss = client.open_by_key(CRM_ID)

        # Bulk-load Sheets metadata: для каждой вкладки получаем hidden_arr.
        ws_by_title = {ws.title: ws for ws in ss.worksheets()
                       if ws.title in SPECIALIST_TABS}
        if opts['tab']:
            ws_by_title = {t: w for t, w in ws_by_title.items()
                           if t == opts['tab']}

        orphans_per_tab: dict[str, list[CrmHawbIndex]] = defaultdict(list)
        total_idx = 0
        total_actual_hidden = 0

        # Bulk-load HAWB БД
        all_entries = list(CrmHawbIndex.objects.filter(
            tab_name__in=list(ws_by_title.keys())))
        hawb_nums = list({e.hawb_number for e in all_entries})
        hawbs_db = {h.hawb_number: h for h in HouseWaybill.objects
                    .filter(hawb_number__in=hawb_nums)}

        # Метаданные hidden по каждой вкладке.
        sheet_hidden: dict[str, list[bool]] = {}
        for title, ws in ws_by_title.items():
            meta = _retry(ss.fetch_sheet_metadata,
                          params={
                              'ranges': title,
                              'fields': 'sheets(properties(title),data(rowMetadata(hiddenByUser)))',
                          },
                          label=f'{title} meta')
            arr = []
            for sh in meta['sheets']:
                if sh['properties']['title'] != title:
                    continue
                data = sh.get('data', [])
                if data:
                    arr = [rm.get('hiddenByUser', False)
                           for rm in data[0].get('rowMetadata', [])]
                    break
            sheet_hidden[title] = arr
            time.sleep(1)

        # Батч-кэш ed_status: _compute_want_hidden зовёт compute_ed_status
        # для каждой скрытой строки (скрытые = выпущенное большинство) —
        # без кэша это тысячи per-HAWB raw_xml-LIKE.
        from cargo.services.alta.ed_status import ed_status_batch
        with ed_status_batch():
            for e in all_entries:
                if e.tab_name not in ws_by_title:
                    continue
                total_idx += 1
                arr = sheet_hidden.get(e.tab_name, [])
                idx = e.row_index - 1
                actual_hidden = arr[idx] if idx < len(arr) else False
                if not actual_hidden:
                    continue  # Sheets не скрыт — пропускаем
                total_actual_hidden += 1
                h = hawbs_db.get(e.hawb_number)
                want_hidden = _compute_want_hidden(e, h)
                if not want_hidden:
                    orphans_per_tab[e.tab_name].append(e)

        total_orphans = sum(len(v) for v in orphans_per_tab.values())
        self.stdout.write(
            f'\nIndex entries scanned: {total_idx}, '
            f'actually hidden in Sheets: {total_actual_hidden}')
        self.stdout.write(f'Orphan hidden (should NOT be): {total_orphans}\n')
        for tab, lst in orphans_per_tab.items():
            self.stdout.write(f'  {tab}: {len(lst)}')
            for e in lst[:10]:
                h = hawbs_db.get(e.hawb_number)
                cs = h.customs_status if h else 'NO_HAWB_IN_DB'
                self.stdout.write(
                    f'    row={e.row_index} hawb={e.hawb_number} '
                    f'cs={cs!r} decl={(h.customs_declaration_number if h else "")!r}')
            if len(lst) > 10:
                self.stdout.write(f'    ... ещё {len(lst) - 10}')

        if not opts['apply'] or not total_orphans:
            if not opts['apply']:
                self.stdout.write('\n--apply not given — отчёт без изменений')
            return

        self.stdout.write('\n=== Applying unhide ===')
        for tab, lst in orphans_per_tab.items():
            ws = ws_by_title[tab]
            rows = sorted({e.row_index for e in lst})

            # Группируем подряд идущие.
            ranges = []
            i = 0
            while i < len(rows):
                start = rows[i]
                end = start
                while i + 1 < len(rows) and rows[i + 1] == end + 1:
                    end = rows[i + 1]
                    i += 1
                i += 1
                ranges.append((start, end))
            requests = [{
                'updateDimensionProperties': {
                    'range': {
                        'sheetId': ws.id, 'dimension': 'ROWS',
                        'startIndex': s - 1, 'endIndex': e,
                    },
                    'properties': {'hiddenByUser': False},
                    'fields': 'hiddenByUser',
                }
            } for s, e in ranges]
            for j in range(0, len(requests), 100):
                _retry(ss.batch_update, {'requests': requests[j:j + 100]},
                       label=f'{tab} unhide')

            # Обновляем индекс.
            CrmHawbIndex.objects.filter(
                pk__in=[e.pk for e in lst]
            ).update(last_hidden=False)

            self.stdout.write(
                f'  {tab}: unhidden {len(rows)} rows '
                f'({len(ranges)} ranges) + index updated')
            time.sleep(2)

        self.stdout.write('\nDone.')
