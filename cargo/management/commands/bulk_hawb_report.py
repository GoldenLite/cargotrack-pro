"""Массовая аналитика по списку HAWB — для проверки большой выборки сразу.

Принимает файл со списком HAWB (один номер на строку) или список через
аргументы. По каждой HAWB собирает компактную сводку из БД, классифицирует
по статусу и пишет в CSV для удобного анализа в Excel.

Запуск:
    # Из файла (рекомендуется для больших списков):
    uv run python manage.py bulk_hawb_report --file hawbs.txt --out report.csv

    # Или прямо через аргументы:
    uv run python manage.py bulk_hawb_report --hawb 10268309640 10269467300 --out r.csv

    # Только сводка в stdout (без CSV):
    uv run python manage.py bulk_hawb_report --file hawbs.txt
"""
from __future__ import annotations

import csv
from collections import Counter

from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill, ImportedSheetRow
from cargo.services.external_warehouse.applier import MOSCOW_CARGO_PREFIXES


def _classify(h, in_sheets: bool) -> str:
    """Категоризация: что у HAWB в БД и нет ли странностей."""
    if not h.mawb_id:
        if (h.svh_do1_sent_at or h.svh_do2_send_at or h.customs_declaration_number
                or h.filed_date or h.release_date):
            return 'BROKEN: данные без mawb'
        return 'ORPHAN: нет mawb, нет данных'

    mawb_num = (h.mawb.awb_number or '').strip() if h.mawb else ''
    pref = mawb_num[:3] if len(mawb_num) >= 4 and mawb_num[3] == '-' else ''
    is_mc = pref in MOSCOW_CARGO_PREFIXES

    if h.customs_status == 'RELEASED':
        if h.customs_declaration_number and h.release_date:
            return 'OK: RELEASED'
        return 'WARN: RELEASED без ДТ/даты'

    if h.customs_status == 'REJECTED':
        return 'OK: REJECTED'
    if h.customs_status == 'HOLD':
        return 'OK: HOLD'

    if is_mc:
        if h.mawb.warehouse_license:
            return 'OK: MC + лицензия'
        return 'WAIT: MC без лицензии (парсер не подцепил)'

    if h.svh_do1_sent_at:
        return 'OK: ДО1 подан'

    if not h.mawb.warehouse_license and not h.svh_do1_sent_at:
        return 'WAIT: нет ни СВХ ни ДО1'

    return 'OK: в работе'


class Command(BaseCommand):
    help = 'Массовая аналитика HAWB по списку'

    def add_arguments(self, parser):
        parser.add_argument('--file', default='', help='Файл со списком HAWB (по одному в строке)')
        parser.add_argument('--hawb', nargs='*', default=[], help='Список HAWB через пробел')
        parser.add_argument('--out', default='', help='CSV-файл с детальной аналитикой')

    def handle(self, *args, **opts):
        hawbs: list[str] = []
        if opts['file']:
            with open(opts['file'], 'r', encoding='utf-8-sig') as f:
                for line in f:
                    s = line.strip()
                    if s and not s.startswith('#'):
                        hawbs.append(s)
        hawbs.extend(opts['hawb'])
        hawbs = [h.strip() for h in hawbs if h.strip()]
        # дедуп с сохранением порядка
        seen = set()
        hawbs = [h for h in hawbs if not (h in seen or seen.add(h))]

        if not hawbs:
            self.stdout.write(self.style.ERROR(
                'Не задан список HAWB. Используй --file или --hawb.'))
            return

        self.stdout.write(f'HAWB в списке: {len(hawbs)}')

        # Прочитаем все нужные HAWB одним запросом
        in_db = {
            h.hawb_number: h for h in HouseWaybill.objects
            .filter(hawb_number__in=hawbs).select_related('mawb')
        }
        in_sheets = set(
            ImportedSheetRow.objects.filter(
                source__kind='general', hawb_number_norm__in=hawbs
            ).values_list('hawb_number_norm', flat=True)
        )

        sheet_rows = {
            r.hawb_number_norm: r for r in
            ImportedSheetRow.objects.filter(
                source__kind='general', hawb_number_norm__in=hawbs
            )
        }

        # Сбор результата
        results: list[dict] = []
        cat_counter: Counter = Counter()
        for hn in hawbs:
            h = in_db.get(hn)
            row = {
                'HAWB': hn,
                'in_db': bool(h),
                'in_sheets': hn in in_sheets,
                'mawb': '',
                'mawb_license': '',
                'logistics_status': '',
                'customs_status': '',
                'customs_declaration_number': '',
                'filed_date': '',
                'release_date': '',
                'svh_do1_sent_at': '',
                'svh_do2_send_at': '',
                'sheet_row_idx': '',
                'category': '',
            }
            if hn in sheet_rows:
                row['sheet_row_idx'] = sheet_rows[hn].source_row_index
            if not h:
                row['category'] = 'NOT_IN_DB'
            else:
                row['mawb'] = h.mawb.awb_number if h.mawb_id and h.mawb else ''
                row['mawb_license'] = h.mawb.warehouse_license if h.mawb_id and h.mawb else ''
                row['logistics_status'] = h.logistics_status or ''
                row['customs_status'] = h.customs_status or ''
                row['customs_declaration_number'] = h.customs_declaration_number or ''
                row['filed_date'] = str(h.filed_date or '')
                row['release_date'] = str(h.release_date or '')
                row['svh_do1_sent_at'] = str(h.svh_do1_sent_at or '')
                row['svh_do2_send_at'] = str(h.svh_do2_send_at or '')
                row['category'] = _classify(h, hn in in_sheets)
            cat_counter[row['category']] += 1
            results.append(row)

        # Сводка
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE('Категории:'))
        for cat, n in sorted(cat_counter.items(), key=lambda x: -x[1]):
            self.stdout.write(f'  {n:>5}  {cat}')

        # CSV
        if opts['out']:
            fields = list(results[0].keys()) if results else []
            with open(opts['out'], 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.DictWriter(f, fieldnames=fields, delimiter=';')
                w.writeheader()
                for r in results:
                    w.writerow(r)
            self.stdout.write(
                self.style.SUCCESS(f'CSV: {opts["out"]} ({len(results)} строк)'))
        else:
            # Краткий вывод первых 30 — для проверки в stdout
            self.stdout.write('')
            self.stdout.write('Первые 30 строк:')
            for r in results[:30]:
                self.stdout.write(
                    f'  {r["HAWB"]:<14} {r["mawb"]:<22} '
                    f'{r["customs_status"] or r["logistics_status"]:<18} '
                    f'{r["category"]}'
                )
            if len(results) > 30:
                self.stdout.write(f'  ... ещё {len(results)-30}')
