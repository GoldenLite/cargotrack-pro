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

import datetime
import logging
import os
import sys
import time
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.utils import timezone as djtz
import gspread.exceptions

from cargo.models import CrmHawbIndex, HouseWaybill
from cargo.services.alta.ed_status import (compute_ed_status,
                                            compute_t_value, ed_status_batch)
from cargo.services.sheets.client import get_client
from cargo.services.sheets.writeback import _customs_requests_text


logger = logging.getLogger('cargo.crm_sync_inc')


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'

# ── Lockfile (тот же паттерн что у auto_sync / delete_to_client_hawbs) ────
# Защита от наложения двух прогонов: если cron-задача запускает нас каждые
# 5 мин, но прошлый прогон ещё крутится (например залип на 429-retry
# Sheets API) — новый запуск выходит без работы. Stale-лимит 30 мин:
# если прошлый прогон висит дольше — его считаем мёртвым, перезахватываем.
_LOCK_DIR = os.path.join(os.path.dirname(sys.executable), '..', '..', 'tmp')
LOCK_PATH = os.path.join(os.path.abspath(_LOCK_DIR), 'crm_sync_incremental.lock')
LOCK_STALE_AFTER_SEC = 30 * 60


def _acquire_lock() -> bool:
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    if os.path.exists(LOCK_PATH):
        try:
            age = time.time() - os.path.getmtime(LOCK_PATH)
        except OSError:
            age = 0
        if age < LOCK_STALE_AFTER_SEC:
            return False
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass
    try:
        with open(LOCK_PATH, 'w') as f:
            f.write(f'pid={os.getpid()} at={datetime.datetime.now().isoformat()}\n')
        return True
    except OSError:
        return False


def _release_lock() -> None:
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except OSError:
        pass

# Стандартный шаблон CRM-tabs (см. crm_sync.py / crm_reindex.py).
COL_HAWB         = 3   # C
COL_ARRIVAL_DATE = 5   # E
COL_WAREHOUSE    = 6   # F
COL_T            = 20  # T (checkbox «подано/в работе/выпущено»)
COL_REQUEST      = 21  # U
COL_DECL         = 23  # W
COL_ED_STATUS    = 24  # X


def _retry(fn, *args, label: str = '', **kwargs):
    import requests.exceptions as _rex
    import urllib3.exceptions as _u3ex
    import ssl as _ssl
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
        except (_rex.SSLError, _rex.ConnectionError, _rex.ChunkedEncodingError,
                _rex.Timeout, _u3ex.MaxRetryError, _u3ex.ProtocolError,
                _ssl.SSLError, OSError) as e:
            # Network/TLS flake — типично SSL: UNEXPECTED_EOF_WHILE_READING
            # от sheets.googleapis.com. Backoff exponential как для API 5xx.
            if attempt < len(backoff):
                wait = backoff[attempt]
                logger.warning('crm_inc %s network err %s: %s, retry in %ds',
                               label, type(e).__name__, str(e)[:120], wait)
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
        parser.add_argument('--no-lock', action='store_true',
                            help='Игнорировать lockfile (для отладки)')

    def handle(self, *args, **opts):
        if not opts.get('no_lock'):
            if not _acquire_lock():
                self.stdout.write(self.style.WARNING(
                    f'Предыдущий запуск ещё работает (lock занят: {LOCK_PATH}). '
                    'Выхожу без работы.'))
                return
        try:
            # Батч-кэш ed_status на весь прогон: убирает per-HAWB
            # raw_xml-LIKE (главный пожиратель дедлайна, см. ed_status.py).
            with ed_status_batch():
                self._run(*args, **opts)
        finally:
            if not opts.get('no_lock'):
                _release_lock()

    # Soft deadline на ВЕСЬ прогон команды. Если за это время не уложились
    # (обычно из-за стабильных 429 от Google Sheets API при конкуренции с
    # auto_sync + agent realtime-writes) — выходим gracefully без записи
    # индекса. Следующий cron-запуск (через 5 мин) перевычислит diff заново
    # и попробует записать. Без этого крутится в _retry-цикле часами
    # (08.06.2026 наблюдали 2ч55мин зависание).
    MAX_TOTAL_SEC = 10 * 60

    def _run(self, *args, **opts):
        t0 = time.time()
        deadline = t0 + self.MAX_TOTAL_SEC

        # Durable-страховка от starvation: голодающие записи (никогда не
        # синканные last_synced_at=NULL, затем самые давние) обрабатываются
        # ПЕРВЫМИ. Если прогон упрётся в дедлайн, урезаются свежесинканные,
        # а не застрявшие выпуски (класс «БД=выпуск, CRM тихо», 08.07.2026).
        # nulls_first: NULL считается «бесконечно старым» → идёт раньше всех.
        from django.db.models import F
        qs = CrmHawbIndex.objects.order_by(
            F('last_synced_at').asc(nulls_first=True))
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

        # sort-proof: читаем ЖИВЫЕ строки (колонка C) всех вкладок в qs ОДИН
        # раз — пишем/скрываем по реальной позиции HAWB, а не по кэшу
        # row_index. Позволяет менеджерам свободно двигать/удалять строки;
        # в чужую строку не пишем. Открываем лист ЗДЕСЬ (раньше — только в
        # фазе записи), переиспользуем ниже.
        from cargo.services.sheets.crm_realtime import live_row_map
        tabs_in_qs = set(qs.values_list('tab_name', flat=True).distinct())
        try:
            client = get_client()
            ss = client.open_by_key(CRM_ID)
            ws_by_title = {ws.title: ws for ws in ss.worksheets()}
        except Exception:
            self.stdout.write(self.style.ERROR(
                '  Не смог открыть CRM spreadsheet — пропуск прогона '
                '(sort-proof требует чтения живых строк)'))
            return
        live_maps: dict = {}
        for _tab in tabs_in_qs:
            _ws = ws_by_title.get(_tab)
            if _ws is not None:
                live_maps[_tab] = live_row_map(_ws)

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

            tab = entry.tab_name
            # sort-proof: реальная строка HAWB в листе; если HAWB больше нет
            # на вкладке (удалена/перемещена) — пропускаем, в чужую строку
            # не пишем (reindex потом уберёт stale-запись индекса).
            row = live_maps.get(tab, {}).get(entry.hawb_number)
            if row is None:
                continue
            changed = False

            # decl: в CRM-вкладке колонка W (рег.номер ДТ) показывает
            # ФАКТ выпуска, а не факт наличия decl в БД. Поэтому пишем
            # ТОЛЬКО при «Выпуск разрешен» (включая суффиксы вида
            # «Выпуск разрешен (Корректировка!)» — substring-match).
            # Все прочие непустые ed_status (Продлен, Идет проверка,
            # Запрошены док-ты, Идет досмотр, Присвоен номер, Открытие
            # процедуры, Отказано в выпуске, Отзыв, Считается не поданной)
            # → стираем W: процедура не завершена выпуском.
            # Исключение — пустой ed_status (legacy ряды без DB-следа,
            # заведённые юзером вручную): не трогаем, чтобы не съесть
            # ручной ввод.
            # Memory: feedback_decl_only_on_released.
            # В таблице «Общее» (общий лист) decl наоборот пишется как
            # только появляется — это другой writeback path.
            if 'Выпуск разрешен' in new_status:
                want_decl = new_decl
            elif not new_status:
                want_decl = entry.last_decl  # legacy — не трогаем
            else:
                want_decl = ''  # любой не-released статус → стираем
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
            # Скрываем только выпущенные (отказ/отзыв ребята продолжают
            # видеть для работы — переподача и т.п.).
            # Legacy: cur_decl без статуса — старая ручная запись «выпущено».
            is_legacy_released = bool(will_decl) and not will_status
            want_hidden = ('Выпуск разрешен' in will_status
                           or is_legacy_released)
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

        # ss / ws_by_title уже открыты выше (для чтения живых строк) —
        # переиспользуем, второй раз не открываем.
        deadline_hit = False
        for tab, updates in updates_per_tab.items():
            if time.time() > deadline:
                deadline_hit = True
                self.stdout.write(self.style.WARNING(
                    f'  TIMEOUT after {self.MAX_TOTAL_SEC}s — '
                    'skip remaining tabs; next cron run retries'))
                break
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

        if not opts['no_hide'] and not deadline_hit:
            for tab in set(list(hide_per_tab.keys()) + list(show_per_tab.keys())):
                if time.time() > deadline:
                    deadline_hit = True
                    self.stdout.write(self.style.WARNING(
                        '  TIMEOUT during hide/show phase — skip remaining'))
                    break
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

        # Bulk-save индекс. Если deadline сработал — НЕ сохраняем, иначе
        # для невышедших вкладок Sheets отстаёт от индекса → next run
        # подумает что эти строки уже синхронизированы. Лучше пересчитать.
        if deadline_hit:
            self.stdout.write(self.style.WARNING(
                f'  index update SKIPPED ({len(idx_to_save)} rows) due to timeout'))
        elif idx_to_save:
            # retry-на-locked: финальный bulk_update конкурирует с agent/
            # auto_sync за write-lock; без повтора прогон падал с
            # database is locked и индекс отставал (см. lock-storm-rootcause).
            from cargo.services.alta.inbox import _retry_on_locked
            _retry_on_locked(
                CrmHawbIndex.objects.bulk_update,
                idx_to_save,
                fields=['last_decl', 'last_status', 'last_request',
                        'last_arrival', 'last_warehouse', 'last_hidden',
                        'last_t', 'last_synced_at'],
                batch_size=500,
            )
            self.stdout.write(f'  index updated: {len(idx_to_save)} rows')

        self.stdout.write(self.style.SUCCESS(
            f'Done. Elapsed: {time.time()-t0:.1f}s'
            + (' (timeout reached)' if deadline_hit else '')))


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
