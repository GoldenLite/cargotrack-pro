"""Аудит: сравнение всех CargoTrack-колонок в Sheets с ожидаемыми из БД.

Идея: для каждого HouseWaybill в Sheets «Общее» считаем какое значение
ДОЛЖНО быть в каждой нашей колонке (по данным БД — собранным из
AltaInboxMessage, AltaOutboxObservation, moscow-cargo парсера), и сверяем
с тем что реально лежит в Sheets. Несоответствия группируем по категориям.

Запуск:
    uv run python manage.py audit_sheets_vs_db                 # отчёт
    uv run python manage.py audit_sheets_vs_db --verbose       # +примеры
    uv run python manage.py audit_sheets_vs_db --csv out.csv   # детально в файл
    uv run python manage.py audit_sheets_vs_db --fix           # авто-фикс
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill, ImportedSheetRow, SheetSource
from cargo.services.sheets.client import open_worksheet
from cargo.services.sheets.writeback import (
    CARGOTRACK_COL_HEADER,
    CARGOTRACK_CARGO_MAWB_HEADER,
    CARGOTRACK_SVH_LICENSE_HEADER,
    CARGOTRACK_SVH_DO1_SENT_HEADER,
    CARGOTRACK_SVH_DATE_HEADER,
    CARGOTRACK_SVH_DO1_HEADER,
    CARGOTRACK_SVH_DO1_WEIGHT_HEADER,
    CARGOTRACK_SVH_DO1_PLACES_HEADER,
    CARGOTRACK_SVH_DO2_DATE_HEADER,
    CARGOTRACK_FILED_DATE_HEADER,
    CARGOTRACK_RELEASE_DATE_HEADER,
    CARGOTRACK_GOODS_COUNT_HEADER,
    CARGOTRACK_CUSTOMS_REQUESTS_HEADER,
    CARGOTRACK_CUSTOMS_REQUESTS_COUNT_HEADER,
    _local_date_str,
    _retry_api,
    _customs_requests_text,
    _customs_requests_count,
)


# Описание полей которые проверяем:
# (column_header, db_provider, label)
# db_provider(hawb) → ожидаемое строковое значение (или '').
def _decl(h):
    return (h.customs_declaration_number or '').strip()


def _mawb(h):
    return (h.mawb.awb_number if h.mawb_id and h.mawb else '') or ''


def _lic(h):
    return (h.mawb.warehouse_license if h.mawb_id and h.mawb else '') or ''


def _scan_into_bond(h):
    return _local_date_str(h.mawb.scan_into_bond if h.mawb_id and h.mawb else None)


def _svh_do1_reg(h):
    return (h.mawb.svh_do1_reg_number if h.mawb_id and h.mawb else '') or ''


def _svh_do1_sent(h):
    return _local_date_str(h.svh_do1_sent_at)


def _svh_do1_weight(h):
    v = h.svh_do1_gross_weight
    if v is None:
        return ''
    # Числа без trailing zeros — как пишет writeback (Decimal → str)
    return str(v)


def _svh_do1_places(h):
    v = h.svh_do1_place_count
    return '' if v is None else str(v)


def _svh_do2(h):
    return _local_date_str(h.svh_do2_send_at)


def _filed(h):
    return _local_date_str(h.filed_date)


def _release(h):
    return _local_date_str(h.release_date)


def _goods_count(h):
    return '' if h.goods_count is None else str(h.goods_count)


def _customs_requests(h):
    return _customs_requests_text(h)


def _customs_requests_count_audit(h):
    return _customs_requests_count(h)


CHECKS = [
    (CARGOTRACK_COL_HEADER,            _decl,             'declaration'),
    (CARGOTRACK_SVH_LICENSE_HEADER,    _lic,              'svh_license'),
    (CARGOTRACK_SVH_DATE_HEADER,       _scan_into_bond,   'svh_do1_reg_date'),
    (CARGOTRACK_SVH_DO1_HEADER,        _svh_do1_reg,      'svh_do1_reg_number'),
    (CARGOTRACK_SVH_DO1_WEIGHT_HEADER, _svh_do1_weight,   'svh_do1_weight'),
    (CARGOTRACK_SVH_DO1_PLACES_HEADER, _svh_do1_places,   'svh_do1_places'),
    (CARGOTRACK_GOODS_COUNT_HEADER,    _goods_count,      'goods_count'),
    (CARGOTRACK_FILED_DATE_HEADER,     _filed,            'filed_date'),
    (CARGOTRACK_RELEASE_DATE_HEADER,   _release,          'release_date'),
    (CARGOTRACK_CUSTOMS_REQUESTS_HEADER,       _customs_requests,
        'customs_requests'),
    (CARGOTRACK_CUSTOMS_REQUESTS_COUNT_HEADER, _customs_requests_count_audit,
        'customs_requests_count'),
    # Удалены 2026-05-26: cargo_mawb, svh_do1_sent_at, svh_do2_send_at —
    # юзер не использует. Поля в БД остаются для внутренней логики.
]


def _col_letter(col_idx: int) -> str:
    result = ''
    n = col_idx
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(ord('A') + rem) + result
    return result


def _normalize_num(s: str) -> str:
    """Sheets любит сохранять 1 как 1.0 — для сравнения нормализуем число."""
    s = (s or '').strip()
    if not s:
        return ''
    # '5,000' → '5.000' → '5'
    s = s.replace(',', '.')
    try:
        d = Decimal(s)
        # Убираем trailing zeros: 5.000 → 5, 0.062 → 0.062
        d = d.normalize()
        return str(d).rstrip('0').rstrip('.') if '.' in str(d) else str(d)
    except Exception:
        return s


class Command(BaseCommand):
    help = 'Сравнить CargoTrack-колонки в Sheets с ожидаемыми из БД'

    def add_arguments(self, parser):
        # NB: --verbose/-v зарезервированы Django, не использовать.
        parser.add_argument('--examples', type=int, default=0,
                            help='Сколько примеров каждой категории показать '
                                 '(0 = только сводка)')
        parser.add_argument('--csv', default='',
                            help='Сохранить полный отчёт в CSV')
        parser.add_argument('--fix', action='store_true',
                            help='Записать правильные значения в Sheets')

    def handle(self, *args, **opts):
        sources = list(SheetSource.objects.filter(kind='general', is_active=True))
        if not sources:
            self.stdout.write('Нет активных general-источников')
            return

        for source in sources:
            self.stdout.write('')
            self.stdout.write(self.style.NOTICE(f'=== {source.name} ==='))
            self._audit_source(source, opts)

    def _audit_source(self, source: SheetSource, opts: dict):
        ws = _retry_api(open_worksheet, source, label='audit open')
        header = _retry_api(ws.row_values, source.header_row, label='audit header')

        # Считаем колонки нашими
        col_map = {}  # header_name → col_idx
        for hdr, _, _ in CHECKS:
            if hdr in header:
                col_map[hdr] = header.index(hdr) + 1
        if not col_map:
            self.stdout.write('  Нет CargoTrack-колонок в шапке, пропуск')
            return

        # Читаем все нужные колонки одним проходом
        col_values = {}
        for hdr, col_idx in col_map.items():
            try:
                col_values[hdr] = _retry_api(ws.col_values, col_idx,
                                             label=f'audit {hdr}')
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f'  Не смог прочитать колонку {hdr}: {e}'))

        # Все HAWB в Sheets «Общее»
        rows = (ImportedSheetRow.objects
                .filter(source=source)
                .exclude(hawb_number_norm=''))
        # Дедуп по hawb — берём самую свежую
        seen = set()
        items = []  # [(row_idx, hawb_number_norm)]
        for r in rows.order_by('-last_imported_at'):
            if r.hawb_number_norm in seen:
                continue
            seen.add(r.hawb_number_norm)
            items.append((r.source_row_index, r.hawb_number_norm))
        self.stdout.write(f'  HAWB в Sheets: {len(items)}')

        # Все HAWB-объекты одной выборкой (батч-prefetch)
        hawbs_db = {
            h.hawb_number: h for h in HouseWaybill.objects
            .filter(hawb_number__in=[hn for _, hn in items])
            .select_related('mawb')
        }

        # Категории несоответствий
        # mismatch_kind → list of (row_idx, hawb_number, sheet_value, db_value, label)
        mismatches: dict = defaultdict(list)
        for row_idx, hn in items:
            h = hawbs_db.get(hn)
            if not h:
                # HAWB в Sheets, но нет в БД → orphan-row, не интересует здесь
                continue
            for hdr, db_fn, label in CHECKS:
                if hdr not in col_map:
                    continue
                values = col_values.get(hdr, [])
                sheet_val = (values[row_idx - 1] if row_idx - 1 < len(values) else '').strip()
                try:
                    db_val = db_fn(h)
                except Exception:
                    db_val = ''
                # Нормализация для числовых колонок
                if label in ('svh_do1_weight', 'svh_do1_places'):
                    s_norm = _normalize_num(sheet_val)
                    d_norm = _normalize_num(db_val)
                else:
                    s_norm = sheet_val
                    d_norm = db_val
                if s_norm == d_norm:
                    continue
                # Категория
                if not d_norm and s_norm:
                    kind = f'STALE: {label}'
                elif d_norm and not s_norm:
                    kind = f'MISSING: {label}'
                else:
                    kind = f'MISMATCH: {label}'
                mismatches[kind].append((row_idx, hn, sheet_val, db_val, label, hdr))

        if not mismatches:
            self.stdout.write(self.style.SUCCESS('  ВСЁ ЧИСТО — несоответствий нет'))
            return

        # Сводка
        self.stdout.write('')
        self.stdout.write(self.style.WARNING('  Несоответствия:'))
        for kind in sorted(mismatches.keys()):
            self.stdout.write(f'    {kind}: {len(mismatches[kind])}')

        # Примеры
        n_examples = opts.get('examples', 0)
        if n_examples:
            self.stdout.write('')
            for kind in sorted(mismatches.keys()):
                self.stdout.write(f'  {kind} (первые {n_examples}):')
                for row_idx, hn, sv, dv, label, hdr in mismatches[kind][:n_examples]:
                    self.stdout.write(
                        f'    row={row_idx} hawb={hn}  Sheets={sv!r}  DB={dv!r}'
                    )

        # CSV
        if opts['csv']:
            with open(opts['csv'], 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.writer(f, delimiter=';')
                w.writerow(['category', 'row_idx', 'hawb_number',
                            'column', 'sheet_value', 'db_value'])
                for kind in sorted(mismatches.keys()):
                    for row_idx, hn, sv, dv, label, hdr in mismatches[kind]:
                        w.writerow([kind, row_idx, hn, hdr, sv, dv])
            self.stdout.write(self.style.SUCCESS(f'  CSV: {opts["csv"]}'))

        # Авто-фикс
        if opts['fix']:
            self.stdout.write('')
            self.stdout.write(self.style.NOTICE('  Авто-фикс...'))
            self._auto_fix(ws, source, col_map, mismatches)

    def _auto_fix(self, ws, source, col_map, mismatches):
        """Группирует все несоответствия в один batch_update."""
        from cargo.services.sheets.writeback import (
            _filter_inrange_updates,
            _chunked_batch_update,
        )
        updates = []
        for kind, items in mismatches.items():
            for row_idx, hn, sv, dv, label, hdr in items:
                col_idx = col_map[hdr]
                letter = _col_letter(col_idx)
                updates.append({
                    'range': f'{letter}{row_idx}',
                    'values': [[dv]],
                })
        if not updates:
            self.stdout.write('  нет updates')
            return
        updates = _filter_inrange_updates(updates, ws, source.name)
        n = _chunked_batch_update(ws, updates, 'audit fix', source.name)
        self.stdout.write(self.style.SUCCESS(f'  записано {n} cells'))
