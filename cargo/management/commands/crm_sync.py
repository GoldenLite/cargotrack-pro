"""CRM-sync: записать decl/ed_status/customs_requests в специалист-вкладки
«Рабочее пространство СТО», скрыть выпущенные, отсортировать по дате прибытия.

Запускается раз в 15 минут (cron).

Колонки в специалист-tabs (стандартный шаблон 23 cols):
  C  3  Номер накладной (HAWB) ← ключ
  E  5  Дата прибытия в РФ (для сортировки)
  U 21  Запрос таможни (ставим дату и текст запроса в одной ячейке)
  W 23  № Декларации на выпуск
  X 24  Статус ЭД (добавляем если нет)
"""
from __future__ import annotations

import logging
from datetime import datetime

from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill
from cargo.services.alta.ed_status import compute_ed_status
from cargo.services.sheets.client import get_client
from cargo.services.sheets.writeback import _customs_requests_text


logger = logging.getLogger('cargo.crm_sync')


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'

# Точный whitelist специалист-вкладок (только эти трогаем).
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

# Ожидаемые заголовки в шапке (для поиска индекса колонки)
HEADER_HAWB         = 'Номер накладной'
HEADER_ARRIVAL_DATE = 'Дата прибытия в РФ'
HEADER_WAREHOUSE    = 'СВХ'
HEADER_REQUEST      = 'Запрос таможни'   # startswith match
HEADER_DECL         = '№ Декларации на выпуск'
HEADER_ED_STATUS    = 'Статус ЭД'

# Если шапка такая же как «обычный шаблон», fallback на жёсткие индексы (1-based).
COL_HAWB         = 3
COL_ARRIVAL_DATE = 5
COL_WAREHOUSE    = 6
COL_REQUEST      = 21
COL_DECL         = 23
COL_ED_STATUS    = 24


def _format_date_only(dt) -> str:
    """aware datetime → 'DD.MM.YYYY' в локальной TZ. None → ''."""
    if not dt:
        return ''
    try:
        from django.utils import timezone as _tz
        local = _tz.localtime(dt) if _tz.is_aware(dt) else dt
        return local.strftime('%d.%m.%Y')
    except Exception:
        return ''


def _col_letter(idx: int) -> str:
    """1-based → буквенная нотация (A=1, Z=26, AA=27...)."""
    s = ''
    n = idx
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _find_col(header: list[str], name: str,
              startswith: bool = False,
              fallback: int = 0) -> int:
    """Возвращает 1-based индекс колонки. Если не найдено — fallback."""
    target = name.strip().lower()
    for i, h in enumerate(header, start=1):
        cur = (h or '').strip().lower()
        if startswith:
            if cur.startswith(target):
                return i
        else:
            if cur == target:
                return i
    return fallback


class Command(BaseCommand):
    help = 'Синхронизировать CRM-вкладки специалистов с DB CargoTrack.'

    def add_arguments(self, parser):
        parser.add_argument('--tab', help='Только этот tab (название)')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--no-hide', action='store_true',
                            help='Не скрывать выпущенные строки')
        parser.add_argument('--no-sort', action='store_true',
                            help='Не сортировать по дате прибытия')

    def handle(self, *args, **opts):
        client = get_client()
        ss = client.open_by_key(CRM_ID)
        self.stdout.write(f'Spreadsheet: {ss.title}')

        target_ws = []
        for ws in ss.worksheets():
            if ws.title not in SPECIALIST_TABS:
                continue
            if opts['tab'] and ws.title != opts['tab']:
                continue
            target_ws.append(ws)

        self.stdout.write(f'Specialist tabs: {len(target_ws)}')

        for ws in target_ws:
            try:
                self._sync_tab(ss, ws, opts)
            except Exception as e:
                logger.exception('crm_sync tab %s failed', ws.title)
                self.stdout.write(self.style.ERROR(
                    f'  {ws.title}: {e}'))

    def _sync_tab(self, ss, ws, opts):
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE(
            f'=== {ws.title}  ({ws.row_count}×{ws.col_count}) ==='))

        header = ws.row_values(1)
        col_hawb      = _find_col(header, HEADER_HAWB,         fallback=COL_HAWB)
        col_arrival   = _find_col(header, HEADER_ARRIVAL_DATE, fallback=COL_ARRIVAL_DATE)
        col_warehouse = _find_col(header, HEADER_WAREHOUSE,    fallback=COL_WAREHOUSE)
        col_request   = _find_col(header, HEADER_REQUEST,      startswith=True,
                                  fallback=COL_REQUEST)
        col_decl      = _find_col(header, HEADER_DECL,         fallback=COL_DECL)
        col_ed        = _find_col(header, HEADER_ED_STATUS,    fallback=0)

        # Добавляем колонку «Статус ЭД» если её нет.
        if not col_ed:
            col_ed = COL_ED_STATUS
            if not opts['dry_run']:
                ws.update_cell(1, col_ed, HEADER_ED_STATUS)
            self.stdout.write(f'  + добавлен заголовок «Статус ЭД» в {_col_letter(col_ed)}1')

        self.stdout.write(
            f'  cols: HAWB={_col_letter(col_hawb)} '
            f'arrival={_col_letter(col_arrival)} '
            f'warehouse={_col_letter(col_warehouse)} '
            f'request={_col_letter(col_request)} '
            f'decl={_col_letter(col_decl)} '
            f'ed_status={_col_letter(col_ed)}')

        # Читаем ВСЕ значения за один запрос — A1:последняя колонка.
        last_col_letter = _col_letter(max(col_hawb, col_arrival, col_warehouse,
                                          col_request, col_decl, col_ed))
        all_vals = ws.get(f'A1:{last_col_letter}{ws.row_count}',
                          value_render_option='UNFORMATTED_VALUE')

        # Карта {row_idx → hawb}
        hawb_rows = {}
        for i, row in enumerate(all_vals[1:], start=2):
            if col_hawb - 1 >= len(row):
                continue
            hn = str(row[col_hawb - 1]).strip()
            if hn:
                hawb_rows[i] = hn

        self.stdout.write(f'  HAWB rows: {len(hawb_rows)}')

        # DB-выборка.
        hawb_numbers = list(set(hawb_rows.values()))
        db_map = {
            h.hawb_number: h
            for h in HouseWaybill.objects.filter(
                hawb_number__in=hawb_numbers
            ).select_related('mawb').prefetch_related('customs_requests')
        }
        self.stdout.write(f'  DB match: {len(db_map)}/{len(hawb_numbers)}')

        # Текущие значения колонок W, X, U, E, F из all_vals — для skip-if-equal.
        updates = []
        rows_hide = []
        rows_show = []
        n_changed_decl = 0
        n_changed_status = 0
        n_changed_request = 0
        n_changed_arrival = 0
        n_changed_warehouse = 0

        for row_idx, hn in hawb_rows.items():
            h = db_map.get(hn)
            row_vals = all_vals[row_idx - 1] if row_idx - 1 < len(all_vals) else []

            cur_decl = (str(row_vals[col_decl - 1]).strip()
                        if col_decl - 1 < len(row_vals) else '')
            cur_status = (str(row_vals[col_ed - 1]).strip()
                          if col_ed - 1 < len(row_vals) else '')
            cur_request = (str(row_vals[col_request - 1]).strip()
                           if col_request - 1 < len(row_vals) else '')
            cur_arrival = (str(row_vals[col_arrival - 1]).strip()
                           if col_arrival - 1 < len(row_vals) else '')
            cur_warehouse = (str(row_vals[col_warehouse - 1]).strip()
                             if col_warehouse - 1 < len(row_vals) else '')

            if h:
                new_decl = (h.customs_declaration_number or '').strip()
                new_status = compute_ed_status(h)
                new_request = _customs_requests_text(h)
                # Дата прибытия = scan_into_bond на mawb-Cargo (ДО1).
                # Лицензия СВХ = mawb.warehouse_license.
                if h.mawb_id and h.mawb:
                    new_arrival = _format_date_only(h.mawb.scan_into_bond)
                    new_warehouse = (h.mawb.warehouse_license or '').strip()
                else:
                    new_arrival = ''
                    new_warehouse = ''

                if cur_decl != new_decl:
                    updates.append({
                        'range': f'{_col_letter(col_decl)}{row_idx}',
                        'values': [[new_decl]],
                    })
                    n_changed_decl += 1
                if cur_status != new_status:
                    updates.append({
                        'range': f'{_col_letter(col_ed)}{row_idx}',
                        'values': [[new_status]],
                    })
                    n_changed_status += 1
                # Запросы / дата прибытия / СВХ пишем только если в DB
                # они непустые — не затираем ручные дополнения ВЭДа.
                if new_request and cur_request != new_request:
                    updates.append({
                        'range': f'{_col_letter(col_request)}{row_idx}',
                        'values': [[new_request]],
                    })
                    n_changed_request += 1
                if new_arrival and cur_arrival != new_arrival:
                    updates.append({
                        'range': f'{_col_letter(col_arrival)}{row_idx}',
                        'values': [[new_arrival]],
                    })
                    n_changed_arrival += 1
                if new_warehouse and cur_warehouse != new_warehouse:
                    updates.append({
                        'range': f'{_col_letter(col_warehouse)}{row_idx}',
                        'values': [[new_warehouse]],
                    })
                    n_changed_warehouse += 1
            else:
                # HAWB нет в БД — критерий hide только по cur_decl (ручной ввод).
                new_decl = cur_decl
                new_status = cur_status

            # Hide-критерий: decl присвоен ИЛИ статус «Выпуск разрешен».
            if new_decl or 'Выпуск разрешен' in new_status:
                rows_hide.append(row_idx)
            else:
                rows_show.append(row_idx)

        self.stdout.write(
            f'  diffs: decl={n_changed_decl} status={n_changed_status} '
            f'request={n_changed_request} arrival={n_changed_arrival} '
            f'svh={n_changed_warehouse}, '
            f'hide={len(rows_hide)} show={len(rows_show)}')

        if opts['dry_run']:
            self.stdout.write('  --dry-run: skip writes')
            return

        # Записываем диффы в Sheets одним batch.
        if updates:
            ws.batch_update(updates, value_input_option='RAW')

        # Применяем hidden state: hide для одних, unhide для других.
        if not opts['no_hide']:
            if rows_hide:
                self._set_hidden(ss, ws, rows_hide, hidden=True)
            if rows_show:
                self._set_hidden(ss, ws, rows_show, hidden=False)

        # Сортируем диапазон.
        if not opts['no_sort']:
            self._sort_by_arrival(ss, ws, col_arrival, last_col_letter)

    def _set_hidden(self, ss, ws, row_indices: list[int], hidden: bool):
        """Устанавливает hiddenByUser=hidden для рядов. Группирует в диапазоны."""
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
                        'sheetId': ws.id,
                        'dimension': 'ROWS',
                        'startIndex': start - 1,  # 0-based
                        'endIndex': end,           # exclusive
                    },
                    'properties': {'hiddenByUser': hidden},
                    'fields': 'hiddenByUser',
                }
            })
        if requests:
            # Chunk по 100 requests чтобы не уперться в лимит Sheets API.
            for i in range(0, len(requests), 100):
                ss.batch_update({'requests': requests[i:i + 100]})
            action = 'hidden' if hidden else 'shown'
            self.stdout.write(f'  {action} {len(row_indices)} rows '
                              f'({len(requests)} ranges)')

    def _sort_by_arrival(self, ss, ws, col_arrival: int,
                         last_col_letter: str):
        """Sort B2:lastcol по столбцу arrival (по возрастанию) + поднимаем
        строки с пустой датой прибытия наверх (после шапки).

        Google sortRange ASC кладёт пустые ячейки В САМЫЙ НИЗ, а юзер хочет
        видеть новые накладные (без даты ДО1) НАВЕРХУ — они «теряются» внизу.

        Алгоритм:
        1. Sort range B2:lastcol по столбцу arrival ascending.
        2. После sort'а строки с пустой arrival оказываются внизу в одном
           подряд блоке. Находим начало этого блока и moveDimension вверх
           сразу после шапки.
        """
        end_col_idx = ord(last_col_letter[-1]) - ord('A') + 1
        if len(last_col_letter) > 1:
            end_col_idx += 26 * (ord(last_col_letter[0]) - ord('A') + 1)
        sort_req = {
            'sortRange': {
                'range': {
                    'sheetId': ws.id,
                    'startRowIndex': 1,        # row 2 (after header)
                    'endRowIndex': ws.row_count,
                    'startColumnIndex': 0,     # column A
                    'endColumnIndex': end_col_idx,
                },
                'sortSpecs': [{
                    'dimensionIndex': col_arrival - 1,
                    'sortOrder': 'ASCENDING',
                }],
            }
        }
        ss.batch_update({'requests': [sort_req]})
        self.stdout.write(f'  sorted by col {_col_letter(col_arrival)} ascending')

        # Перемещаем пустые-arrival ряды вверх.
        arrival_col = ws.col_values(col_arrival)
        # Считаем строки с HAWB-номером в col_hawb но пустой arrival.
        # Идём с конца — empty группируется внизу после sort'а.
        # Находим первую пустую снизу.
        empty_start = None
        for i in range(len(arrival_col) - 1, 0, -1):
            v = (arrival_col[i] or '').strip()
            if v:
                empty_start = i + 2  # row_idx (1-based, после v)
                break
        # Если все пустые — empty_start = 2. Если все заполнены — None.
        if empty_start is None:
            # все пустые — sort не нужен, выходим
            return
        # empty_start = первая пустая строка снизу.
        # Но мы хотим строки С HAWB но без даты — это они «теряются».
        # Без HAWB строки — пустые трейлеры, не трогаем.
        # Проверим: реально ли это пустые-arrival ряды с HAWB?
        if empty_start > ws.row_count:
            return  # все строки имеют arrival

        # Находим самую нижнюю строку С HAWB номером.
        hawb_col = ws.col_values(COL_HAWB)
        last_hawb_row = 1
        for i in range(len(hawb_col) - 1, 0, -1):
            if (hawb_col[i] or '').strip():
                last_hawb_row = i + 1
                break
        if empty_start > last_hawb_row:
            return  # нет HAWB без даты — нечего поднимать

        # Перемещаем строки empty_start..last_hawb_row в верх (после шапки).
        move_req = {
            'moveDimension': {
                'source': {
                    'sheetId': ws.id,
                    'dimension': 'ROWS',
                    'startIndex': empty_start - 1,  # 0-based
                    'endIndex': last_hawb_row,       # exclusive
                },
                'destinationIndex': 1,  # после шапки (row 2)
            }
        }
        ss.batch_update({'requests': [move_req]})
        n_moved = last_hawb_row - empty_start + 1
        self.stdout.write(f'  moved {n_moved} empty-arrival rows to top')
