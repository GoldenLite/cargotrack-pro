"""Аудит ed_status в CRM-вкладках «Рабочее пространство СТО».

Для каждой HAWB в Sheets:
  - читаем колонку X (статус ЭД) — реальное значение
  - вычисляем compute_ed_status(h) из БД — ожидаемое
  - сравниваем

Категории:
  - MATCH: одинаковые
  - SHEET_EMPTY: Sheets пустой, DB говорит надо что-то писать
  - DIFFERENT: значения отличаются
  - NO_DB: HAWB нет в БД (non-DB, пропускаем)

Использование:
    manage.py audit_crm_ed_status
    manage.py audit_crm_ed_status --tab "Беляева Екатерина"
    manage.py audit_crm_ed_status --fix          # записать DB → Sheets
    manage.py audit_crm_ed_status --examples 5
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict

from django.core.management.base import BaseCommand
import gspread.exceptions

from cargo.models import HouseWaybill
from cargo.services.alta.ed_status import compute_ed_status
from cargo.services.sheets.client import get_client


logger = logging.getLogger('cargo.audit_crm_ed')


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'

from cargo.services.sheets.crm_tabs import SPECIALIST_TABS  # noqa: E402  единый whitelist вкладок

COL_HAWB      = 3   # C
COL_ED_STATUS = 24  # X


def _col_letter(idx: int) -> str:
    s = ''
    n = idx
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _retry(fn, *args, label: str = '', **kwargs):
    backoff = [1, 2, 4, 8, 16, 32]
    for attempt in range(len(backoff) + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(e.response, 'status_code', None)
            if status in (429, 500, 502, 503, 504) and attempt < len(backoff):
                wait = backoff[attempt]
                logger.warning('audit %s API %s, retry in %ds',
                               label, status, wait)
                time.sleep(wait)
                continue
            raise


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--tab', help='Только эта вкладка')
        parser.add_argument('--fix', action='store_true',
                            help='Записать правильные значения в Sheets')
        parser.add_argument('--fix-only', default='',
                            help='CSV категорий для fix: '
                                 'SHEET_EMPTY,DB_EMPTY,DIFFERENT')
        parser.add_argument('--examples', type=int, default=3,
                            help='Сколько примеров категории показать')

    def handle(self, *args, **opts):
        client = get_client()
        ss = client.open_by_key(CRM_ID)
        self.stdout.write(f'Spreadsheet: {ss.title}')

        target = []
        for ws in ss.worksheets():
            if ws.title not in SPECIALIST_TABS:
                continue
            if opts['tab'] and ws.title != opts['tab']:
                continue
            target.append(ws)
        self.stdout.write(f'Tabs: {len(target)}')

        grand_total = defaultdict(int)
        for i, ws in enumerate(target):
            try:
                self._audit_tab(ws, opts, grand_total)
            except Exception as e:
                logger.exception('audit_crm_ed_status tab %s failed', ws.title)
                self.stdout.write(self.style.ERROR(f'  {ws.title}: {e}'))
            if i + 1 < len(target):
                time.sleep(3)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=== TOTAL ==='))
        for k, v in grand_total.items():
            self.stdout.write(f'  {k}: {v}')

    def _audit_tab(self, ws, opts, grand_total):
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE(f'=== {ws.title} ==='))

        rng = f'A1:{_col_letter(COL_ED_STATUS)}{ws.row_count}'
        all_vals = _retry(ws.get, rng,
                          value_render_option='UNFORMATTED_VALUE',
                          label=f'{ws.title} get')

        # Сбор HAWB → (row_idx, cur_status)
        hawb_rows = {}  # hawb_number -> (row_idx, cur_status)
        for i, row in enumerate(all_vals[1:], start=2):
            if COL_HAWB - 1 >= len(row):
                continue
            hn = str(row[COL_HAWB - 1]).strip()
            if not hn:
                continue
            cur_status = (str(row[COL_ED_STATUS - 1]).strip()
                          if COL_ED_STATUS - 1 < len(row) else '')
            hawb_rows[hn] = (i, cur_status)

        self.stdout.write(f'  HAWB rows: {len(hawb_rows)}')

        # DB bulk
        hawbs_db = {
            h.hawb_number: h for h in HouseWaybill.objects
            .filter(hawb_number__in=list(hawb_rows.keys()))
            .select_related('mawb')
        }
        self.stdout.write(f'  DB match: {len(hawbs_db)}/{len(hawb_rows)}')

        stats = defaultdict(int)
        examples = defaultdict(list)
        fixes_by_cat: dict[str, list] = defaultdict(list)

        for hn, (row_idx, cur_status) in hawb_rows.items():
            h = hawbs_db.get(hn)
            if not h:
                stats['NO_DB'] += 1
                continue
            try:
                expected = compute_ed_status(h)
            except Exception:
                expected = ''
            if expected == cur_status:
                stats['MATCH'] += 1
                continue
            if not cur_status and expected:
                cat = 'SHEET_EMPTY'
            elif cur_status and not expected:
                cat = 'DB_EMPTY'
            else:
                cat = 'DIFFERENT'
            stats[cat] += 1
            if len(examples[cat]) < opts['examples']:
                examples[cat].append(
                    f'{hn} row={row_idx}: sheet={cur_status!r} db={expected!r}')
            fixes_by_cat[cat].append((row_idx, expected))

        for k, v in stats.items():
            grand_total[k] += v
            self.stdout.write(f'  {k}: {v}')
        for cat, ex in examples.items():
            for e in ex:
                self.stdout.write(f'    {cat} → {e}')

        if opts['fix']:
            # Какие категории fix'им
            only = {s.strip() for s in (opts.get('fix_only') or '').split(',')
                    if s.strip()}
            apply_fixes = []
            for cat, items in fixes_by_cat.items():
                if only and cat not in only:
                    continue
                apply_fixes.extend(items)
            if apply_fixes:
                updates = [{
                    'range': f'{_col_letter(COL_ED_STATUS)}{row}',
                    'values': [[exp]],
                } for row, exp in apply_fixes]
                CHUNK = 100
                for i in range(0, len(updates), CHUNK):
                    _retry(ws.batch_update, updates[i:i + CHUNK],
                           value_input_option='USER_ENTERED',
                           label=f'{ws.title} fix {i//CHUNK + 1}')
                self.stdout.write(f'  FIXED: {len(apply_fixes)} cells')
                grand_total['FIXED'] += len(apply_fixes)
