"""Стирает ПРЕЖДЕВРЕМЕННУЮ «Дату прибытия в РФ» в CRM-вкладках.

Декларанты вписывают дату заранее (груз ещё на складе отправки/в пути),
в колонку, которая должна быть ФАКТической. Эта команда раз в день
подчищает такие «призрачные» даты по жёсткому критерию:

  - строка НЕ скрыта (скрытые = выпущенные, дата верная — не трогаем);
  - HAWB есть в нашей БД (вне-БД клиентские ручные даты не трогаем);
  - груз по статусу ЕЩЁ НЕ в РФ (logistics_status ∈ NOT_ARRIVED);
  - нет ДО1 (scan_into_bond пуст), нет ДТ, нет выпуска.

Когда груз реально прибудет (ДО1 → scan_into_bond), crm_sync_incremental
проставит верную дату сам. sort-proof: таргет по живой колонке C.

Запуск:
    manage.py prune_premature_arrival             # dry-run (только показать)
    manage.py prune_premature_arrival --apply     # стереть
    manage.py prune_premature_arrival --apply --max 300
"""
import logging
import re
from collections import defaultdict

from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill, CrmHawbIndex
from cargo.services.sheets.crm_realtime import (
    COL_HAWB, COL_ARRIVAL_DATE, _retry)
from cargo.services.sheets.client import get_client


logger = logging.getLogger('cargo.prune_arrival')

CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'
HAWB_RE = re.compile(r'^\d{11}$')

from cargo.services.sheets.crm_tabs import SPECIALIST_TABS  # noqa: E402  единый whitelist вкладок

# Логистические статусы «груз ещё НЕ в РФ» (до ARRIVED_DEST).
NOT_ARRIVED = {
    'CREATED', 'TO_ORIGIN_WH', 'AT_ORIGIN_WH', 'CONSOLIDATED',
    'READY_TO_SHIP', 'EXPORT_CUSTOMS', 'IN_TRANSIT_EXP',
}


class Command(BaseCommand):
    help = 'Стирает преждевременную дату прибытия в CRM (груз ещё не в РФ).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Реально стереть (без флага — dry-run)')
        parser.add_argument('--tab', help='Только эта вкладка')
        parser.add_argument('--max', type=int, default=200,
                            help='Sanity-порог: больше — не стираем, алерт')

    def _hidden(self, ss, ws):
        meta = _retry(ss.fetch_sheet_metadata, params={
            'ranges': ws.title,
            'fields': 'sheets(properties(title),data(rowMetadata(hiddenByUser)))',
        }, label=f'{ws.title} meta')
        for sh in meta['sheets']:
            if sh['properties']['title'] == ws.title:
                d = sh.get('data', [])
                if d:
                    return [rm.get('hiddenByUser', False)
                            for rm in d[0].get('rowMetadata', [])]
        return []

    def handle(self, *args, **opts):
        client = get_client()
        ss = client.open_by_key(CRM_ID)
        targets = defaultdict(list)   # tab -> [(ws, row, hawb)]
        n = 0
        for ws in ss.worksheets():
            if ws.title not in SPECIALIST_TABS:
                continue
            if opts['tab'] and ws.title != opts['tab']:
                continue
            colc = ws.col_values(COL_HAWB)
            cole = ws.col_values(COL_ARRIVAL_DATE)
            hid = self._hidden(ss, ws)
            rows = []
            for i, v in enumerate(colc):
                hn = str(v or '').strip()
                if not HAWB_RE.match(hn):
                    continue
                e = str(cole[i] if i < len(cole) else '').strip()
                if not e:
                    continue
                if i < len(hid) and hid[i]:        # скрытая — пропуск
                    continue
                rows.append((i + 1, hn))
            if not rows:
                continue
            hawbs = {h.hawb_number: h for h in HouseWaybill.objects.filter(
                hawb_number__in=[hn for _, hn in rows]).select_related('mawb')}
            for r, hn in rows:
                h = hawbs.get(hn)
                if h is None:
                    continue                        # вне БД — не трогаем
                if h.logistics_status not in NOT_ARRIVED:
                    continue                        # груз уже в РФ
                scan = h.mawb.scan_into_bond if (h.mawb_id and h.mawb) else None
                if scan is not None:
                    continue
                if (h.customs_status == 'RELEASED'
                        or (h.customs_declaration_number or '').strip()
                        or h.release_date is not None):
                    continue                        # груз реально оформлялся
                targets[ws.title].append((ws, r, hn))
                n += 1

        self.stdout.write(f'Преждевременных дат: {n}')
        for tab, items in targets.items():
            for ws, r, hn in items[:20]:
                self.stdout.write(f'  {tab} row={r} {hn}')

        if not opts['apply']:
            self.stdout.write('--dry-run: ничего не стёрто.')
            return
        if n > opts['max']:
            logger.warning('prune_arrival: %d > max %d — НЕ стираю (проверь критерий)',
                           n, opts['max'])
            self.stdout.write(self.style.WARNING(
                f'СТОП: {n} > порога {opts["max"]} — не стираю (--max чтобы поднять)'))
            return

        total = 0
        for tab, items in targets.items():
            ws = items[0][0]
            updates = [{'range': f'E{r}', 'values': [['']]} for _, r, _2 in items]
            for i in range(0, len(updates), 100):
                _retry(ws.batch_update, updates[i:i + 100],
                       value_input_option='USER_ENTERED', label=f'{tab} prune')
            hns = [hn for _, _2, hn in items]
            idx = list(CrmHawbIndex.objects.filter(tab_name=tab, hawb_number__in=hns))
            for e in idx:
                e.last_arrival = ''
            if idx:
                CrmHawbIndex.objects.bulk_update(idx, ['last_arrival'])
            total += len(updates)
            logger.info('prune_arrival: %s стёрто %d дат', tab, len(updates))
        self.stdout.write(self.style.SUCCESS(f'Стёрто {total} преждевременных дат.'))
