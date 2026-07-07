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
    CARGOTRACK_ATTEMPTS_COUNT_HEADER,
    CARGOTRACK_ED_STATUS_HEADER,
    EXPORT_DECLARATION_HEADER,
    EXPORT_DECLARATION_FORM_HEADER,
    EXPORT_DECLARANT_HEADER,
    EXPORT_ED_STATUS_HEADER,
    EXPORT_FILED_DATE_HEADER,
    EXPORT_RELEASE_DATE_HEADER,
    EXPORT_GOODS_COUNT_HEADER,
    EXPORT_CUSTOMS_REQUESTS_HEADER,
    EXPORT_CUSTOMS_REQUESTS_COUNT_HEADER,
    EXPORT_ATTEMPTS_COUNT_HEADER,
    EXPORT_TRANSPORT_DOC_HEADER,
    _local_date_str,
    _retry_api,
    _hawb_live_rows,
    _customs_requests_text,
    _customs_requests_count,
    _attempts_count,
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
    # Как пишет writeback (_format_weight): без trailing zeros, десятичный
    # разделитель — ЗАПЯТАЯ (RU-локаль листа; с точкой USER_ENTERED
    # оставляет текст и ломает сортировку). Сравнение гасится _normalize_num.
    from decimal import Decimal
    try:
        d = Decimal(v).normalize()
        return (str(d) if d != 0 else '0').replace('.', ',')
    except Exception:
        return str(v).replace('.', ',')


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


def _decl_form(h):
    return (h.declaration_form or '').strip()


def _declarant(h):
    return (h.declarant_name or '').strip()


def _transport_doc(h):
    return (h.mawb.awb_number if h.mawb_id and h.mawb else '') or ''


EXPORT_CHECKS = [
    (EXPORT_DECLARATION_HEADER,        _decl,             'declaration'),
    (EXPORT_DECLARATION_FORM_HEADER,   _decl_form,        'declaration_form'),
    (EXPORT_DECLARANT_HEADER,          _declarant,        'declarant'),
    (EXPORT_FILED_DATE_HEADER,         _filed,            'filed_date'),
    (EXPORT_RELEASE_DATE_HEADER,       _release,          'release_date'),
    (EXPORT_GOODS_COUNT_HEADER,        _goods_count,      'goods_count'),
    (EXPORT_TRANSPORT_DOC_HEADER,      _transport_doc,    'transport_doc'),
    (EXPORT_CUSTOMS_REQUESTS_HEADER,
        lambda h: _customs_requests_text(h),  'customs_requests'),
    (EXPORT_CUSTOMS_REQUESTS_COUNT_HEADER,
        lambda h: str(_customs_requests_count(h) or ''), 'customs_requests_count'),
    (EXPORT_ATTEMPTS_COUNT_HEADER,
        lambda h: str(_attempts_count(h) or ''), 'attempts_count'),
    (EXPORT_ED_STATUS_HEADER,
        lambda h: __import__('cargo.services.alta.ed_status',
                             fromlist=['compute_ed_status']).compute_ed_status(h),
        'ed_status'),
]


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
    (CARGOTRACK_ATTEMPTS_COUNT_HEADER,         lambda h: _attempts_count(h),
        'attempts_count'),
    (CARGOTRACK_ED_STATUS_HEADER,
        lambda h: __import__('cargo.services.alta.ed_status',
                             fromlist=['compute_ed_status']).compute_ed_status(h),
        'ed_status'),
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


# Колонки-даты: writeback пишет '09:03:31', но Google Sheets при сохранении
# срезает ведущий ноль часа → '9:03:31'. Без нормализации аудит видит вечный
# MISMATCH и --fix переписывает их каждый прогон впустую (format-churn).
_DATE_LABELS = frozenset({
    'filed_date', 'release_date', 'svh_do1_reg_date',
    'svh_do1_sent', 'svh_do2',
})


def _normalize_dt(s: str) -> str:
    """Нормализует дату/время: убирает ведущие нули у КАЖДОГО числового
    компонента ('10.06.2026 09:03:31' → '10.6.2026 9:3:31'). Применяется к
    обеим сторонам, поэтому реальные расхождения (другой день/час) сохраняются,
    а различие в нулях/формате — гасится."""
    s = (s or '').strip()
    if not s:
        return ''
    # Разбиваем по разделителям, сохраняя их; числовые токены → int()
    return ''.join(
        str(int(tok)) if tok.isdigit() else tok
        for tok in re.split(r'([.\s:/-])', s)
    )


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
        parser.add_argument('--kind', default='general',
                            choices=['general', 'export'],
                            help='Тип SheetSource (general или export)')
        parser.add_argument('--force', action='store_true',
                            help='Bypass freshness gate (для --fix). По умолчанию '
                                 'если ImportedSheetRow устарел >5 мин — fail. '
                                 'Защита от инцидента 09.06.2026 (--fix '
                                 'писал в устаревшие row_index после ручной '
                                 'сортировки юзера → данные съезжали).')
        parser.add_argument('--max-stale-min', type=int, default=5,
                            help='Max возраст import_sheets для --fix (минут, '
                                 'default 5)')

    def handle(self, *args, **opts):
        kind = opts.get('kind') or 'general'
        sources = list(SheetSource.objects.filter(kind=kind, is_active=True))
        if not sources:
            self.stdout.write(f'Нет активных {kind}-источников')
            return

        # Freshness: ИСТОРИЧЕСКИ тут был hard-fail если import_sheets устарел
        # >5мин — защита от инцидента 09.06.2026 (--fix писал в устаревшие
        # row_index после ручной сортировки → данные съезжали). Теперь аудит
        # sort-proof (таргетит ЖИВУЮ строку HAWB, _hawb_live_rows), поэтому
        # staleness больше не опасен — оставляем только предупреждение, чтобы
        # 15-минутный CargoTrack-AuditFix реально залечивал, а не падал на gate.
        # Настоящая защита теперь в _audit_source: --fix пропускается, если
        # живую HAWB-колонку прочитать не удалось (live_rows пуст).
        if opts.get('fix') and not opts.get('force'):
            from django.db.models import Max
            from django.utils import timezone as _tz
            import datetime as _dt
            max_stale = int(opts.get('max_stale_min') or 5)
            cutoff = _tz.now() - _dt.timedelta(minutes=max_stale)
            for source in sources:
                last = (ImportedSheetRow.objects
                        .filter(source=source)
                        .aggregate(m=Max('last_imported_at'))['m'])
                if last is None:
                    self.stdout.write(self.style.WARNING(
                        f'  [{source.name}] нет ImportedSheetRow — запусти '
                        f'import_sheets (источник будет пропущен)'))
                elif last < cutoff:
                    age_min = int((_tz.now() - last).total_seconds() / 60)
                    self.stdout.write(self.style.WARNING(
                        f'  [{source.name}] import_sheets {age_min} мин назад — '
                        f'аудит sort-proof таргетит живые строки, продолжаю'))

        # Батч-кэш ed_status: per-cargo пред-агрегация вместо per-HAWB
        # raw_xml-LIKE — без него аудит на 14k+ HAWB не влезал в лимит
        # крона (kill 267014, инцидент 04-07.07.2026).
        from cargo.services.alta.ed_status import ed_status_batch
        with ed_status_batch():
            for source in sources:
                self.stdout.write('')
                self.stdout.write(self.style.NOTICE(f'=== {source.name} ==='))
                self._audit_source(source, opts)

    def _audit_source(self, source: SheetSource, opts: dict):
        checks = EXPORT_CHECKS if source.kind == 'export' else CHECKS
        ws = _retry_api(open_worksheet, source, label='audit open')
        header = _retry_api(ws.row_values, source.header_row, label='audit header')

        # Считаем колонки нашими
        col_map = {}  # header_name → col_idx
        for hdr, _, _ in checks:
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

        # Sort-proof: где HAWB реально сейчас в листе (а не по устаревшему
        # кэшу source_row_index). Без этого пересортировка/сдвиг строк юзером
        # даёт ложные MISSING/STALE пары, а --fix пишет в чужие строки
        # (инцидент 09.06.2026). Таргетим по живой колонке HAWB, как writeback.
        live_rows = _hawb_live_rows(ws, source.header_row)

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
            # Sort-proof: реальная строка HAWB в листе, fallback на кэш.
            if live_rows:
                row_idx = live_rows.get(hn, row_idx)
            h = hawbs_db.get(hn)
            if not h:
                # HAWB в Sheets, но нет в БД → orphan-row, не интересует здесь
                continue
            for hdr, db_fn, label in checks:
                if hdr not in col_map:
                    continue
                values = col_values.get(hdr, [])
                sheet_val = (values[row_idx - 1] if row_idx - 1 < len(values) else '').strip()
                try:
                    db_val = db_fn(h)
                except Exception:
                    db_val = ''
                # Нормализация для числовых и date-колонок (иначе ложные
                # mismatch'и: Sheets хранит 1 как 1.0, а час как 9 а не 09).
                if label in ('svh_do1_weight', 'svh_do1_places'):
                    s_norm = _normalize_num(sheet_val)
                    d_norm = _normalize_num(db_val)
                elif label in _DATE_LABELS:
                    s_norm = _normalize_dt(sheet_val)
                    d_norm = _normalize_dt(db_val)
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
            # Sort-proof guard: без живой HAWB-колонки --fix опирался бы на
            # устаревшие row_index → запись в чужие строки. Пропускаем
            # (если только не --force).
            if not live_rows and not opts.get('force'):
                self.stdout.write(self.style.ERROR(
                    '  Живую HAWB-колонку прочитать не удалось — --fix пропущен '
                    '(sort-proof недоступен; --force чтобы записать на свой риск)'))
                return
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
