"""Полный re-index CRM-вкладок «Рабочее пространство СТО».

Читает все 9 специалист-вкладок, для каждой HAWB обновляет CrmHawbIndex:
- если HAWB уже в индексе на этой вкладке — обновляет row_index +
  last_decl/status/request/arrival/warehouse/hidden из ТЕКУЩИХ Sheets
  значений (это становится baseline для incremental).
- если новая — создаёт запись.
- если HAWB ушла из вкладки (была, нет в текущем читке) — удаляет
  запись (HAWB могла быть удалена или перемещена).

Запускать раз в день ночью (или после большого ручного редактирования
Sheets юзером).

Использование:
    manage.py crm_reindex
    manage.py crm_reindex --tab "Беляева Екатерина"
"""
from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand
from django.utils import timezone as djtz
import gspread.exceptions

from cargo.models import CrmHawbIndex
from cargo.services.sheets.client import get_client


logger = logging.getLogger('cargo.crm_reindex')


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

# Стандартный шаблон CRM-tabs (см. crm_sync.py).
COL_HAWB         = 3   # C
COL_ARRIVAL_DATE = 5   # E
COL_WAREHOUSE    = 6   # F
COL_T            = 20  # T (checkbox «подано/в работе/выпущено»)
COL_REQUEST      = 21  # U
COL_DECL         = 23  # W
COL_ED_STATUS    = 24  # X


def _retry(fn, *args, label: str = '', **kwargs):
    backoff = [1, 2, 4, 8, 16, 32]
    for attempt in range(len(backoff) + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(e.response, 'status_code', None)
            if status in (429, 500, 502, 503, 504) and attempt < len(backoff):
                wait = backoff[attempt]
                logger.warning('crm_reindex %s API %s, retry in %ds',
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
    help = 'Полный reindex CRM-вкладок в CrmHawbIndex.'

    def add_arguments(self, parser):
        parser.add_argument('--tab', help='Только эта вкладка')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        client = get_client()
        ss = client.open_by_key(CRM_ID)
        self.stdout.write(f'Spreadsheet: {ss.title}')

        target_tabs = []
        for ws in ss.worksheets():
            if ws.title not in SPECIALIST_TABS:
                continue
            if opts['tab'] and ws.title != opts['tab']:
                continue
            target_tabs.append(ws)
        self.stdout.write(f'Specialist tabs: {len(target_tabs)}')

        n_total = 0
        for i, ws in enumerate(target_tabs):
            try:
                n_total += self._reindex_tab(ws, opts)
            except Exception as e:
                logger.exception('crm_reindex tab %s failed', ws.title)
                self.stdout.write(self.style.ERROR(f'  {ws.title}: {e}'))
            if i + 1 < len(target_tabs):
                time.sleep(5)

        self.stdout.write(self.style.SUCCESS(
            f'\nTotal indexed HAWB rows: {n_total}'))

    def _reindex_tab(self, ws, opts):
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE(f'=== {ws.title} ==='))

        last_col = max(COL_HAWB, COL_ARRIVAL_DATE, COL_WAREHOUSE,
                       COL_REQUEST, COL_DECL, COL_ED_STATUS)
        last_letter = _col_letter(last_col)
        rng = f'A1:{last_letter}{ws.row_count}'
        all_vals = _retry(ws.get, rng,
                          value_render_option='UNFORMATTED_VALUE',
                          label=f'{ws.title} get')

        # Также вытаскиваем hidden state per row через batchGet metadata.
        # Это дороже, чем просто значения, но даёт точный last_hidden.
        # На первом этапе можем не дёргать — last_hidden=False default,
        # incremental после первого прогона его перетрёт.

        # Собираем HAWB → (row_idx, decl, status, request, arrival, warehouse)
        found: dict[str, dict] = {}
        for i, row in enumerate(all_vals[1:], start=2):
            if COL_HAWB - 1 >= len(row):
                continue
            hn = str(row[COL_HAWB - 1]).strip()
            if not hn:
                continue

            def _cell(idx):
                v = (row[idx - 1] if idx - 1 < len(row) else '')
                return str(v).strip() if v not in (None, '') else ''

            t_raw = (row[COL_T - 1] if COL_T - 1 < len(row) else None)
            t_val = bool(t_raw) if t_raw is not None else False

            found[hn] = {
                'row_index': i,
                'last_decl':      _cell(COL_DECL),
                'last_status':    _cell(COL_ED_STATUS),
                'last_request':   _cell(COL_REQUEST),
                'last_arrival':   _cell(COL_ARRIVAL_DATE),
                'last_warehouse': _cell(COL_WAREHOUSE),
                'last_t':         t_val,
            }

        self.stdout.write(f'  HAWB found: {len(found)}')

        if opts['dry_run']:
            self.stdout.write('  --dry-run: skip writes')
            return len(found)

        # Удаляем из индекса HAWB, которых уже нет в этой вкладке.
        existing = set(CrmHawbIndex.objects.filter(tab_name=ws.title)
                       .values_list('hawb_number', flat=True))
        removed = existing - set(found.keys())
        if removed:
            CrmHawbIndex.objects.filter(
                tab_name=ws.title, hawb_number__in=removed,
            ).delete()
            self.stdout.write(f'  removed (no longer in tab): {len(removed)}')

        # Bulk upsert — избегаем update_or_create который дёргает
        # select_for_update и лочит SQLite-WAL.
        now = djtz.now()
        existing = {
            e.hawb_number: e for e in
            CrmHawbIndex.objects.filter(tab_name=ws.title)
        }

        to_create: list[CrmHawbIndex] = []
        to_update: list[CrmHawbIndex] = []
        for hn, d in found.items():
            ex = existing.get(hn)
            if ex is None:
                to_create.append(CrmHawbIndex(
                    hawb_number=hn,
                    tab_name=ws.title,
                    row_index=d['row_index'],
                    last_decl=d['last_decl'][:64],
                    last_status=d['last_status'][:128],
                    last_request=d['last_request'],
                    last_arrival=d['last_arrival'][:16],
                    last_warehouse=d['last_warehouse'][:32],
                    last_t=d['last_t'],
                ))
            else:
                ex.row_index = d['row_index']
                ex.last_decl = d['last_decl'][:64]
                ex.last_status = d['last_status'][:128]
                ex.last_request = d['last_request']
                ex.last_arrival = d['last_arrival'][:16]
                ex.last_warehouse = d['last_warehouse'][:32]
                ex.last_t = d['last_t']
                ex.last_seen_at = now
                to_update.append(ex)

        if to_create:
            CrmHawbIndex.objects.bulk_create(to_create, batch_size=500,
                                             ignore_conflicts=True)
        if to_update:
            CrmHawbIndex.objects.bulk_update(
                to_update,
                fields=['row_index', 'last_decl', 'last_status',
                        'last_request', 'last_arrival', 'last_warehouse',
                        'last_t', 'last_seen_at'],
                batch_size=500,
            )

        self.stdout.write(f'  created={len(to_create)} updated={len(to_update)}')
        return len(found)
