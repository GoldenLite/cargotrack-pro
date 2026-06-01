"""Rollback v2: откатить propagation-victims по более точному критерию.

Старый rollback_propagate_cargo пропускал HAWB, у которых есть inbox-msg
(например CMN.13021 SVH-msg c FK), но msg не упоминает ни HAWB ни decl.

Новый критерий: НИ ОДИН inbox-msg.raw_xml не содержит и hawb_number И
declaration_number одновременно. Если такого msg нет → decl был
пропагирован cargo-level пропагацией, чистим.

Дополнительно: чистим Sheets-ячейки W (декларация) и X (статус ЭД) во
ВСЕХ CRM-вкладках (Рабочее пространство СТО) — иначе hide-критерий
«есть decl + нет статуса» снова скроет очищенные HAWB.

Использование:
  manage.py rollback_propagate_v2 --dry-run
  manage.py rollback_propagate_v2 --hawbs 10245997263,10254258024,...
  manage.py rollback_propagate_v2 --hawbs-file hawbs.txt
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from django.core.management.base import BaseCommand
import gspread.exceptions

from cargo.models import AltaInboxMessage, HawbDeclarationAttempt, HouseWaybill


logger = logging.getLogger('cargo.rollback_v2')


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

# Стандартный шаблон CRM-tabs.
COL_HAWB = 3      # C
COL_DECL = 23     # W
COL_ED_STATUS = 24  # X


def _retry(fn, *args, label: str = '', **kwargs):
    backoff = [1, 2, 4, 8, 16, 32]
    for attempt in range(len(backoff) + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(e.response, 'status_code', None)
            if status in (429, 500, 502, 503, 504) and attempt < len(backoff):
                wait = backoff[attempt]
                logger.warning('rollback_v2 %s API %s, retry in %ds',
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
    help = 'Rollback v2 propagation-victims + clear CRM cells.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--hawbs', help='CSV list of HAWB numbers')
        parser.add_argument('--hawbs-file', help='File with HAWB numbers (one per line)')
        parser.add_argument('--skip-crm', action='store_true',
                            help='Skip CRM Sheets cleanup (только БД + Общее)')
        parser.add_argument('--only-crm', action='store_true',
                            help='Только Sheets cleanup, БД не трогать')

    def handle(self, *args, **opts):
        # Сбор кандидатов
        explicit = set()
        if opts['hawbs']:
            explicit.update(s.strip() for s in opts['hawbs'].split(',') if s.strip())
        if opts['hawbs_file']:
            p = Path(opts['hawbs_file'])
            with p.open() as f:
                for line in f:
                    s = line.strip()
                    if s and not s.startswith('#'):
                        explicit.add(s)

        if explicit:
            qs = HouseWaybill.objects.filter(hawb_number__in=explicit)
            self.stdout.write(f'Explicit list: {len(explicit)} HAWB')
        else:
            qs = HouseWaybill.objects.filter(customs_status='RELEASED').exclude(
                customs_declaration_number='')
            self.stdout.write(f'Scanning all RELEASED: {qs.count()}')

        victims: list[HouseWaybill] = []
        for h in qs.only(
            'pk', 'hawb_number', 'customs_status', 'customs_declaration_number',
            'filed_date', 'release_date',
        ):
            if not h.hawb_number:
                continue
            decl = (h.customs_declaration_number or '').strip()
            # Если HAWB уже очищена — пропускаем
            if h.customs_status != 'RELEASED' and not decl:
                continue
            # Проверка: есть ли inbox-msg с raw_xml содержащим И HAWB И decl?
            if decl:
                has_both = AltaInboxMessage.objects.filter(
                    raw_xml__icontains=h.hawb_number,
                ).filter(raw_xml__icontains=decl).exists()
                if has_both:
                    continue
            victims.append(h)

        self.stdout.write(f'Victims to rollback: {len(victims)}')
        for h in victims[:50]:
            self.stdout.write(
                f'  {h.hawb_number}  decl={h.customs_declaration_number}  '
                f'release={h.release_date}')
        if len(victims) > 50:
            self.stdout.write(f'  ... ещё {len(victims) - 50}')

        if opts['dry_run']:
            self.stdout.write('--dry-run: stop')
            return

        if not victims:
            self.stdout.write('Nothing to do.')
            return

        # 1) Очистка БД
        if not opts['only_crm']:
            self._rollback_db(victims)

        # 2) Writeback Общее (пишет '' в W/X через стандартные batch)
        if not opts['only_crm'] and not opts['skip_crm']:
            self._writeback_general(victims)

        # 3) Очистка CRM-вкладок (W + X)
        if not opts['skip_crm']:
            self._clear_crm_cells(victims)

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. rolled_back={len(victims)}'))

    def _rollback_db(self, victims: list[HouseWaybill]):
        self.stdout.write('Updating DB...')
        n = 0
        for h in victims:
            decl = h.customs_declaration_number
            HouseWaybill.objects.filter(pk=h.pk).update(
                customs_status='',
                customs_declaration_number='',
                filed_date=None,
                release_date=None,
            )
            # Удаляем propagation attempt
            if decl:
                HawbDeclarationAttempt.objects.filter(
                    hawb=h, declaration_number=decl,
                ).delete()
            n += 1
        self.stdout.write(f'  DB cleared: {n}')

    def _writeback_general(self, victims: list[HouseWaybill]):
        from cargo.services.sheets.writeback import (
            batch_write_declarations_for_hawbs,
            batch_write_release_dates_for_hawbs,
            batch_write_filed_dates_for_hawbs,
            batch_write_ed_status_for_hawbs,
            batch_write_attempts_count_for_hawbs,
        )
        self.stdout.write('Writeback to Общее (clears W/X/dates)...')
        for h in victims:
            h.refresh_from_db()
        try:
            batch_write_declarations_for_hawbs(victims)
            batch_write_release_dates_for_hawbs(victims)
            batch_write_filed_dates_for_hawbs(victims)
            batch_write_ed_status_for_hawbs(victims)
            batch_write_attempts_count_for_hawbs(victims)
            self.stdout.write('  Общее: writeback done')
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'  Общее writeback: {e}'))

    def _clear_crm_cells(self, victims: list[HouseWaybill]):
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

        self.stdout.write(f'Clearing CRM cells in {len(SPECIALIST_TABS)} tabs...')
        for i, ws in enumerate(ss.worksheets()):
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
            # Clear W (decl) and X (ed_status) if not already empty
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
            self.stdout.write(f'  {ws.title}: 0 cells to clear')
            return

        CHUNK = 100
        for i in range(0, len(updates), CHUNK):
            chunk = updates[i:i + CHUNK]
            _retry(ws.batch_update, chunk,
                   value_input_option='USER_ENTERED',
                   label=f'{ws.title} clear {i//CHUNK + 1}')
        self.stdout.write(f'  {ws.title}: cleared {len(updates)} cells')
