"""Полностью удалить ряды (deleteDimension) для списка HAWB из всех
CRM-вкладок специалистов «Рабочее пространство СТО».

Берёт HAWB из CrmHawbIndex (быстро, без скана Sheets), удаляет ряд
в Sheets через deleteDimension и сразу удаляет запись из индекса.

Удаление per-tab делаем в порядке row_index DESC, чтоб удаление не
сдвинуло индексы остальных рядов в той же вкладке.

Использование:
    manage.py delete_hawb_rows 10123456789 10987654321
    manage.py delete_hawb_rows --file hawbs.txt
    manage.py delete_hawb_rows --file hawbs.txt --dry-run
"""
from collections import defaultdict

from django.core.management.base import BaseCommand
import gspread.exceptions
import time
import logging

from cargo.models import CrmHawbIndex
from cargo.services.sheets.client import get_client


logger = logging.getLogger('cargo.delete_hawb_rows')


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'


def _retry(fn, *args, label='', **kwargs):
    backoff = [1, 2, 4, 8, 16, 32]
    for attempt in range(len(backoff) + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(e.response, 'status_code', None)
            if status in (429, 500, 502, 503, 504) and attempt < len(backoff):
                wait = backoff[attempt]
                logger.warning('delete_hawb_rows %s API %s, retry %ds',
                               label, status, wait)
                time.sleep(wait)
                continue
            raise


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='*',
                            help='HAWB номера (через пробел)')
        parser.add_argument('--file', help='Файл со списком HAWB (1 на строку)')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        targets = set(opts['hawbs'] or [])
        if opts['file']:
            with open(opts['file'], encoding='utf-8') as f:
                for line in f:
                    s = line.strip().split()[0] if line.strip() else ''
                    if s and not s.startswith('#'):
                        targets.add(s)
        targets = {t.strip() for t in targets if t.strip()}
        self.stdout.write(f'Target HAWB: {len(targets)}')
        if not targets:
            return

        # Группируем entries по tab
        entries_by_tab: dict[str, list[CrmHawbIndex]] = defaultdict(list)
        for e in CrmHawbIndex.objects.filter(hawb_number__in=targets):
            entries_by_tab[e.tab_name].append(e)

        if not entries_by_tab:
            self.stdout.write('  В индексе ни одной из этих HAWB не найдено.')
            return

        client = get_client()
        ss = client.open_by_key(CRM_ID)
        ws_by_title = {w.title: w for w in ss.worksheets()}

        total_deleted = 0
        for tab, entries in entries_by_tab.items():
            ws = ws_by_title.get(tab)
            if not ws:
                self.stdout.write(self.style.WARNING(
                    f'  {tab}: вкладка не найдена в spreadsheet'))
                continue

            # Сортируем по убыванию row_index (deleteDimension сдвигает
            # последующие ряды на -1, поэтому удаляем сначала самые нижние).
            entries_sorted = sorted(entries, key=lambda e: e.row_index,
                                    reverse=True)
            self.stdout.write(self.style.NOTICE(
                f'\n=== {tab}: удаляем {len(entries_sorted)} ряд(а) ==='))
            for e in entries_sorted[:15]:
                self.stdout.write(
                    f'  row={e.row_index} hawb={e.hawb_number}')
            if len(entries_sorted) > 15:
                self.stdout.write(
                    f'  ... ещё {len(entries_sorted) - 15}')

            if opts['dry_run']:
                continue

            # Группируем в подряд идущие диапазоны (опять же DESC)
            requests = [{
                'deleteDimension': {
                    'range': {
                        'sheetId': ws.id,
                        'dimension': 'ROWS',
                        'startIndex': e.row_index - 1,
                        'endIndex': e.row_index,
                    }
                }
            } for e in entries_sorted]

            # Sheets API позволяет до 100 ops в одном batchUpdate.
            CHUNK = 100
            for i in range(0, len(requests), CHUNK):
                _retry(ss.batch_update,
                       {'requests': requests[i:i + CHUNK]},
                       label=f'{tab} chunk {i//CHUNK + 1}')

            # Удаляем entries из индекса.
            CrmHawbIndex.objects.filter(
                tab_name=tab,
                pk__in=[e.pk for e in entries_sorted],
            ).delete()

            total_deleted += len(entries_sorted)
            self.stdout.write(
                f'  Sheets: удалено {len(entries_sorted)} рядов, '
                f'индекс: чищен')
            time.sleep(2)  # cooldown между вкладками

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. Удалено всего: {total_deleted} рядов'))
