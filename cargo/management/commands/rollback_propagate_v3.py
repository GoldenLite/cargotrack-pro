"""Rollback v3: O(N+M) auto-scan через regex по raw_xml.

v2 делал 1491 × icontains = 2982 full-table scan'ов ≈ 50 мин.
v3 за один проход по AltaInboxMessage строит set legit (hawb, decl) пар,
потом для каждой RELEASED HAWB проверяет membership за O(1).

Regex pattern для HAWB: 10-11 цифр (\d{10,11}).
Regex pattern для decl: 8/6/7 цифр (\d{8}/\d{6}/\d{7}).

Если в одном msg нашли HAWB=A и decl=D — добавляем (A,D) в legit set.
Это «А упомянут в msg где есть D» — корреляция, не доказательство, но
для propagation-detection достаточно: рег.номер ДТ упоминается ТОЛЬКО
в msg которые относятся к этой ДТ.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from django.core.management.base import BaseCommand
import gspread.exceptions

from cargo.models import AltaInboxMessage, HawbDeclarationAttempt, HouseWaybill


logger = logging.getLogger('cargo.rollback_v3')


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

COL_HAWB = 3
COL_DECL = 23
COL_ED_STATUS = 24

HAWB_RE = re.compile(r'\b(\d{10,11})\b')
DECL_RE = re.compile(r'\b(\d{8}/\d{6}/\d{7})\b')


def _retry(fn, *args, label: str = '', **kwargs):
    backoff = [1, 2, 4, 8, 16, 32]
    for attempt in range(len(backoff) + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(e.response, 'status_code', None)
            if status in (429, 500, 502, 503, 504) and attempt < len(backoff):
                wait = backoff[attempt]
                logger.warning('rollback_v3 %s API %s, retry in %ds',
                               label, status, wait)
                time.sleep(wait)
                continue
            raise


def _col_letter(idx: int) -> str:
    s = ''
    n = idx
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class Command(BaseCommand):
    help = 'Rollback v3 — auto-scan через regex (O(N+M)).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--limit', type=int, default=0,
                            help='Ограничить количество rolled-back HAWB '
                                 '(safety, 0 = без ограничения)')
        parser.add_argument('--skip-crm', action='store_true')
        parser.add_argument('--save-list', help='Сохранить victim list в файл')

    def handle(self, *args, **opts):
        t0 = time.time()

        # Phase 1: построить legit set
        self.stdout.write('Phase 1: scanning AltaInboxMessage.raw_xml...')
        legit: set[tuple[str, str]] = set()
        msg_qs = (AltaInboxMessage.objects
                  .only('raw_xml')
                  .iterator(chunk_size=500))
        n_msgs = 0
        for m in msg_qs:
            n_msgs += 1
            raw = m.raw_xml or ''
            if not raw:
                continue
            hawbs = set(HAWB_RE.findall(raw))
            decls = set(DECL_RE.findall(raw))
            if not (hawbs and decls):
                continue
            for h in hawbs:
                for d in decls:
                    legit.add((h, d))
            if n_msgs % 5000 == 0:
                self.stdout.write(
                    f'  scanned {n_msgs} msgs, legit pairs: {len(legit)} '
                    f'(elapsed {time.time()-t0:.0f}s)')

        self.stdout.write(
            f'Phase 1 done: {n_msgs} msgs, {len(legit)} legit pairs '
            f'({time.time()-t0:.0f}s)')

        # Phase 2: найти victims
        self.stdout.write('Phase 2: checking RELEASED HAWB...')
        qs = HouseWaybill.objects.filter(customs_status='RELEASED').exclude(
            customs_declaration_number='').only(
            'pk', 'hawb_number', 'customs_status',
            'customs_declaration_number', 'filed_date', 'release_date')

        victims: list[HouseWaybill] = []
        n_checked = 0
        for h in qs.iterator(chunk_size=500):
            n_checked += 1
            if not h.hawb_number:
                continue
            decl = (h.customs_declaration_number or '').strip()
            if not decl:
                continue
            if (h.hawb_number, decl) in legit:
                continue
            victims.append(h)

        self.stdout.write(
            f'Phase 2 done: {n_checked} RELEASED scanned, '
            f'{len(victims)} victims ({time.time()-t0:.0f}s)')

        if opts['save_list']:
            with open(opts['save_list'], 'w') as f:
                for h in victims:
                    f.write(f'{h.hawb_number}\n')
            self.stdout.write(f'Saved victim list to {opts["save_list"]}')

        if opts['limit'] and len(victims) > opts['limit']:
            self.stdout.write(
                f'Limiting to first {opts["limit"]} victims (safety)')
            victims = victims[:opts['limit']]

        self.stdout.write(f'\nTo rollback: {len(victims)}')
        for h in victims[:30]:
            self.stdout.write(
                f'  {h.hawb_number}  decl={h.customs_declaration_number}')
        if len(victims) > 30:
            self.stdout.write(f'  ... ещё {len(victims) - 30}')

        if opts['dry_run']:
            self.stdout.write('\n--dry-run: stop')
            return

        if not victims:
            return

        # Phase 3: clear DB
        self.stdout.write('\nPhase 3: clearing DB...')
        for h in victims:
            decl = h.customs_declaration_number
            HouseWaybill.objects.filter(pk=h.pk).update(
                customs_status='',
                customs_declaration_number='',
                filed_date=None,
                release_date=None,
            )
            if decl:
                HawbDeclarationAttempt.objects.filter(
                    hawb=h, declaration_number=decl,
                ).delete()
        self.stdout.write(f'  DB cleared: {len(victims)}')

        # Phase 4: writeback Общее
        self.stdout.write('\nPhase 4: writeback Общее...')
        from cargo.services.sheets.writeback import (
            batch_write_declarations_for_hawbs,
            batch_write_release_dates_for_hawbs,
            batch_write_filed_dates_for_hawbs,
            batch_write_ed_status_for_hawbs,
            batch_write_attempts_count_for_hawbs,
        )
        for h in victims:
            h.refresh_from_db()
        try:
            batch_write_declarations_for_hawbs(victims)
            batch_write_release_dates_for_hawbs(victims)
            batch_write_filed_dates_for_hawbs(victims)
            batch_write_ed_status_for_hawbs(victims)
            batch_write_attempts_count_for_hawbs(victims)
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'  writeback error: {e}'))

        # Phase 5: CRM cleanup
        if not opts['skip_crm']:
            self.stdout.write('\nPhase 5: CRM cleanup...')
            self._clear_crm_cells(victims)

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. rolled_back={len(victims)} elapsed={time.time()-t0:.0f}s'))

    def _clear_crm_cells(self, victims):
        from cargo.services.sheets.client import get_client

        hawb_set = {h.hawb_number for h in victims if h.hawb_number}
        if not hawb_set:
            return

        client = get_client()
        try:
            ss = client.open_by_key(CRM_ID)
        except gspread.exceptions.APIError as e:
            self.stdout.write(self.style.ERROR(f'CRM open: {e}'))
            return

        for ws in ss.worksheets():
            if ws.title not in SPECIALIST_TABS:
                continue
            try:
                self._clear_one_tab(ws, hawb_set)
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f'  {ws.title}: {e}'))
            time.sleep(3)

    def _clear_one_tab(self, ws, hawb_set: set):
        last_col = max(COL_DECL, COL_ED_STATUS)
        rng = f'A2:{_col_letter(last_col)}{ws.row_count}'
        try:
            all_vals = _retry(ws.get, rng,
                              value_render_option='UNFORMATTED_VALUE',
                              label=f'{ws.title} get')
        except gspread.exceptions.APIError as e:
            self.stdout.write(self.style.ERROR(
                f'  {ws.title}: get failed: {e}'))
            return

        updates = []
        for i, row in enumerate(all_vals, start=2):
            if COL_HAWB - 1 >= len(row):
                continue
            hn = str(row[COL_HAWB - 1]).strip()
            if hn not in hawb_set:
                continue
            cur_decl = (str(row[COL_DECL - 1]).strip()
                        if COL_DECL - 1 < len(row) else '')
            cur_status = (str(row[COL_ED_STATUS - 1]).strip()
                          if COL_ED_STATUS - 1 < len(row) else '')
            if cur_decl:
                updates.append({
                    'range': f'{_col_letter(COL_DECL)}{i}',
                    'values': [['']],
                })
            if cur_status:
                updates.append({
                    'range': f'{_col_letter(COL_ED_STATUS)}{i}',
                    'values': [['']],
                })

        if not updates:
            self.stdout.write(f'  {ws.title}: 0 cells')
            return

        CHUNK = 100
        for i in range(0, len(updates), CHUNK):
            chunk = updates[i:i + CHUNK]
            _retry(ws.batch_update, chunk,
                   value_input_option='USER_ENTERED',
                   label=f'{ws.title} clear {i//CHUNK + 1}')
        self.stdout.write(f'  {ws.title}: cleared {len(updates)}')
