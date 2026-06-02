"""Incremental sync CRM-вкладок через CrmHawbIndex.

Не читает Sheets — берёт last_* из индекса, сравнивает с текущим
DB state, пишет только diff-ячейки. Скорость — секунды вместо
минут (типичный прогон должен укладываться в 5-30 сек).

Что делает:
  1. Bulk-load всех HAWB из CrmHawbIndex (одной выборкой через
     hawb_number__in).
  2. Для каждой записи в индексе:
     - compute new_decl, new_status, new_request, new_arrival, new_warehouse
     - сравнить с last_*
     - если diff — добавить в очередь записи
     - обновить last_* в индексе
  3. Hide/show ряд по will-state (decl + status после sync).
     - track в индексе last_hidden, изменения через batchUpdate.
  4. Sort: НЕ делает (отдельная команда crm_sort_all).

Не нужен полный re-scan вкладки — индекс знает все строки. Если новая
HAWB появилась в Sheets вручную — её в индексе нет, будет добавлена
следующим crm_reindex (ночью). Это норм: ребятам важна автоматизация
для УЖЕ известных HAWB, а новые добавляются в Sheets по факту получения
ДО1, что уже синхронизировано на нашей стороне.

Использование:
    manage.py crm_sync_incremental
    manage.py crm_sync_incremental --tab "Беляева Екатерина"
    manage.py crm_sync_incremental --dry-run
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.utils import timezone as djtz
import gspread.exceptions

from cargo.models import CrmHawbIndex, HouseWaybill
from cargo.services.alta.ed_status import compute_ed_status, compute_t_value
from cargo.services.sheets.client import get_client
from cargo.services.sheets.writeback import _customs_requests_text


logger = logging.getLogger('cargo.crm_sync_inc')


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'

# Стандартный шаблон CRM-tabs (см. crm_sync.py / crm_reindex.py).
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
                logger.warning('crm_inc %s API %s, retry in %ds',
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


def _format_date_only(dt) -> str:
    if not dt:
        return ''
    try:
        from django.utils import timezone as _tz
        local = _tz.localtime(dt) if _tz.is_aware(dt) else dt
        return local.strftime('%d.%m.%Y')
    except Exception:
        return ''


class Command(BaseCommand):
    help = 'Incremental sync CRM-вкладок через CrmHawbIndex.'

    def add_arguments(self, parser):
        parser.add_argument('--tab', help='Только эта вкладка')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--no-hide', action='store_true',
                            help='Не менять hidden state')

    def handle(self, *args, **opts):
        t0 = time.time()

        qs = CrmHawbIndex.objects.all()
        if opts['tab']:
            qs = qs.filter(tab_name=opts['tab'])

        n_idx = qs.count()
        self.stdout.write(f'CRM index entries: {n_idx}')
        if not n_idx:
            self.stdout.write('Пусто. Сначала запусти crm_reindex.')
            return

        # Bulk-load HAWB по hawb_number__in.
        hawb_numbers = list(qs.values_list('hawb_number', flat=True).distinct())
        hawbs_db = {
            h.hawb_number: h for h in HouseWaybill.objects
            .filter(hawb_number__in=hawb_numbers)
            .select_related('mawb')
            .prefetch_related('customs_requests')
        }
        self.stdout.write(f'DB match: {len(hawbs_db)}/{len(hawb_numbers)}')

        # Группируем updates per-tab.
        updates_per_tab: dict[str, list] = defaultdict(list)
        hide_per_tab:    dict[str, list] = defaultdict(list)
        show_per_tab:    dict[str, list] = defaultdict(list)
        idx_to_save:     list[CrmHawbIndex] = []
        n_diff_decl = 0
        n_diff_status = 0
        n_diff_request = 0
        n_diff_arrival = 0
        n_diff_warehouse = 0
        n_diff_hidden = 0
        n_diff_t = 0

        for entry in qs.iterator(chunk_size=500):
            h = hawbs_db.get(entry.hawb_number)
            if h:
                new_decl = (h.customs_declaration_number or '').strip()
                new_status = compute_ed_status(h)
                new_request = _customs_requests_text(h)
                if h.mawb_id and h.mawb:
                    new_arrival = _format_date_only(h.mawb.scan_into_bond)
                    new_warehouse = (h.mawb.warehouse_license or '').strip()
                else:
                    new_arrival = ''
                    new_warehouse = ''
                # T checkbox: TRUE если в пайплайне таможни, FALSE при
                # отказе/отзыве/не подано.
                new_t = compute_t_value(h)
                db_tracked = True
            else:
                # HAWB нет в БД — ячейки не трогаем, но hide-критерий
                # вычисляем по cur (last_*) значениям. Это ловит legacy:
                # decl стоит в Sheets без статуса = старая ручная запись
                # «выпущено».
                new_decl = entry.last_decl
                new_status = entry.last_status
                new_request = entry.last_request
                new_arrival = entry.last_arrival
                new_warehouse = entry.last_warehouse
                # Для non-DB не трогаем T (это вручную ставили).
                new_t = entry.last_t
                db_tracked = False

            row = entry.row_index
            tab = entry.tab_name
            changed = False

            # decl: пишем только при выпуске, иначе очищаем (пропагация
            # cleanup, см. логику в crm_sync.py).
            if 'Выпуск разрешен' in new_status:
                want_decl = new_decl
            elif not new_decl:
                want_decl = ''
            else:
                want_decl = entry.last_decl  # не трогаем
            if want_decl != entry.last_decl:
                updates_per_tab[tab].append({
                    'range': f'{_col_letter(COL_DECL)}{row}',
                    'values': [[want_decl]],
                })
                entry.last_decl = want_decl[:64]
                changed = True
                n_diff_decl += 1

            # ed_status: всегда пишем DB.
            if new_status != entry.last_status:
                updates_per_tab[tab].append({
                    'range': f'{_col_letter(COL_ED_STATUS)}{row}',
                    'values': [[new_status]],
                })
                entry.last_status = new_status[:128]
                changed = True
                n_diff_status += 1

            # request: пишем только если непустой и отличается.
            if new_request and new_request != entry.last_request:
                updates_per_tab[tab].append({
                    'range': f'{_col_letter(COL_REQUEST)}{row}',
                    'values': [[new_request]],
                })
                entry.last_request = new_request
                changed = True
                n_diff_request += 1

            # arrival: пишем только если непустой и отличается.
            if new_arrival and new_arrival != entry.last_arrival:
                updates_per_tab[tab].append({
                    'range': f'{_col_letter(COL_ARRIVAL_DATE)}{row}',
                    'values': [[new_arrival]],
                })
                entry.last_arrival = new_arrival[:16]
                changed = True
                n_diff_arrival += 1

            # warehouse: пишем только если непустой и отличается.
            if new_warehouse and new_warehouse != entry.last_warehouse:
                updates_per_tab[tab].append({
                    'range': f'{_col_letter(COL_WAREHOUSE)}{row}',
                    'values': [[new_warehouse]],
                })
                entry.last_warehouse = new_warehouse[:32]
                changed = True
                n_diff_warehouse += 1

            # T checkbox: пишем для DB-tracked HAWB всегда, для non-DB
            # не трогаем (new_t = entry.last_t).
            if db_tracked and new_t != entry.last_t:
                updates_per_tab[tab].append({
                    'range': f'{_col_letter(COL_T)}{row}',
                    'values': [[new_t]],
                })
                entry.last_t = new_t
                changed = True
                n_diff_t += 1

            # hide-критерий по will-state:
            will_decl = entry.last_decl  # уже обновлён выше
            will_status = entry.last_status
            # Финальные состояния — HAWB больше не в работе:
            #   Выпуск разрешен / Отказано / Отзыв / Считается не поданной.
            # Legacy: cur_decl без статуса — старая ручная запись «выпущено».
            is_final = any(m in will_status for m in
                           ('Выпуск разрешен', 'Отказ', 'Отзыв',
                            'Считается не поданной'))
            is_legacy_released = bool(will_decl) and not will_status
            want_hidden = is_final or is_legacy_released
            if want_hidden != entry.last_hidden:
                if want_hidden:
                    hide_per_tab[tab].append(row)
                else:
                    show_per_tab[tab].append(row)
                entry.last_hidden = want_hidden
                changed = True
                n_diff_hidden += 1

            if changed:
                entry.last_synced_at = djtz.now()
                idx_to_save.append(entry)

        self.stdout.write(
            f'  diffs: decl={n_diff_decl} status={n_diff_status} '
            f'request={n_diff_request} arrival={n_diff_arrival} '
            f'svh={n_diff_warehouse} t={n_diff_t}, hidden={n_diff_hidden}')

        if opts['dry_run']:
            self.stdout.write('  --dry-run: skip writes')
            self.stdout.write(f'Elapsed: {time.time()-t0:.1f}s')
            return

        if not (updates_per_tab or hide_per_tab or show_per_tab):
            self.stdout.write('Nothing to write.')
            self.stdout.write(f'Elapsed: {time.time()-t0:.1f}s')
            return

        client = get_client()
        ss = client.open_by_key(CRM_ID)
        ws_by_title = {ws.title: ws for ws in ss.worksheets()}

        for tab, updates in updates_per_tab.items():
            ws = ws_by_title.get(tab)
            if not ws:
                self.stdout.write(self.style.WARNING(
                    f'  {tab}: not found in spreadsheet, skip'))
                continue
            CHUNK = 100
            for i in range(0, len(updates), CHUNK):
                _retry(ws.batch_update, updates[i:i + CHUNK],
                       value_input_option='USER_ENTERED',
                       label=f'{tab} batch {i//CHUNK + 1}')
            self.stdout.write(f'  {tab}: wrote {len(updates)} cells')

        if not opts['no_hide']:
            for tab in set(list(hide_per_tab.keys()) + list(show_per_tab.keys())):
                ws = ws_by_title.get(tab)
                if not ws:
                    continue
                rows_h = hide_per_tab.get(tab, [])
                rows_s = show_per_tab.get(tab, [])
                requests = (_build_dim_requests(ws.id, rows_h, True)
                            + _build_dim_requests(ws.id, rows_s, False))
                if requests:
                    _retry(ss.batch_update, {'requests': requests},
                           label=f'{tab} hide/show')
                    self.stdout.write(
                        f'  {tab}: hide={len(rows_h)} show={len(rows_s)}')

        # Bulk-save индекс.
        if idx_to_save:
            CrmHawbIndex.objects.bulk_update(
                idx_to_save,
                fields=['last_decl', 'last_status', 'last_request',
                        'last_arrival', 'last_warehouse', 'last_hidden',
                        'last_t', 'last_synced_at'],
                batch_size=500,
            )
            self.stdout.write(f'  index updated: {len(idx_to_save)} rows')

        self.stdout.write(self.style.SUCCESS(
            f'Done. Elapsed: {time.time()-t0:.1f}s'))


def _build_dim_requests(sheet_id: int, row_indices: list[int],
                        hidden: bool) -> list[dict]:
    """Группирует подряд идущие row_indices в один updateDimensionProperties."""
    sorted_rows = sorted(set(row_indices))
    requests = []
    i = 0
    while i < len(sorted_rows):
        start = sorted_rows[i]
        end = start
        while i + 1 < len(sorted_rows) and sorted_rows[i + 1] == end + 1:
            end = sorted_rows[i + 1]
            i += 1
        i += 1
        requests.append({
            'updateDimensionProperties': {
                'range': {
                    'sheetId': sheet_id,
                    'dimension': 'ROWS',
                    'startIndex': start - 1,
                    'endIndex': end,
                },
                'properties': {'hiddenByUser': hidden},
                'fields': 'hiddenByUser',
            }
        })
    return requests
