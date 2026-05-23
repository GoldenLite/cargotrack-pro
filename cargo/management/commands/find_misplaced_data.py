"""Сканирует свободные текстовые поля HAWB/Cargo на структурированные данные,
которые должны были попасть в спец-поля.

Ищет узнаваемые форматы (лицензия СВХ, номер ДТ, ДО1, MAWB, дата) внутри:
- HouseWaybill.notes
- HouseWaybill.problem_note
- HouseWaybill.tsd_number     (вдруг там не ТСД, а ДТ)
- Cargo.description / description_ru
- Cargo.warehouse_name        (вдруг там не имя, а лицензия)

И докладывает HAWB/Cargo, у которых:
  а) В тексте найден формат X, но соответствующее поле X пустое — кандидат на перенос
  б) В тексте найден формат X, и поле X тоже заполнено — потенциально дубль / расхождение

Используется как разведка перед массовым remap'ом данных. НИЧЕГО НЕ ПИШЕТ.

Запуск:
    uv run python manage.py find_misplaced_data
    uv run python manage.py find_misplaced_data --limit 100
"""
from __future__ import annotations

import re
from collections import defaultdict

from django.core.management.base import BaseCommand

from cargo.models import Cargo, HouseWaybill


# Лицензия СВХ: «10001/060324/10009/1» или варианты без последнего числа.
# 5-8 цифр / 6 цифр (ddmmyy) / 4-7 цифр / опц. /1-3 цифры
LICENSE_RE = re.compile(r'\b\d{5,8}/\d{6}/\d{4,7}(?:/\d{1,3})?\b')

# Номер ДТ: «10005020/170426/0084406» — 8/6/7 цифр
DECL_RE = re.compile(r'\b\d{8}/\d{6}/\d{7}\b')

# MAWB: «784-12345678» (3-8)
MAWB_RE = re.compile(r'\b\d{3}-\d{8}\b')

# Даты в распространённых форматах
DATE_RE = re.compile(r'\b\d{2}[.\-/]\d{2}[.\-/](?:20)?\d{2}\b')


def scan_text(text: str) -> dict[str, list[str]]:
    """Возвращает {pattern_name: [matches]} для всех найденных форматов."""
    text = text or ''
    found = {}
    for name, regex in [
        ('license', LICENSE_RE),
        ('decl',    DECL_RE),
        ('mawb',    MAWB_RE),
        ('date',    DATE_RE),
    ]:
        m = regex.findall(text)
        if m:
            found[name] = m
    return found


class Command(BaseCommand):
    help = ('Разведка: ищет структурированные данные (лицензия/ДТ/MAWB/дата) '
            'в свободных полях HAWB/Cargo')

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0,
                            help='Лимит выдачи на каждую категорию')

    def handle(self, *args, **opts):
        limit = opts['limit']
        # ── HouseWaybill ────────────────────────────────────────
        self.stdout.write(self.style.NOTICE('=== HouseWaybill.notes ==='))
        hits = self._scan_hawb_field('notes')
        self._report_hawb(hits, limit)

        self.stdout.write('')
        self.stdout.write(self.style.NOTICE('=== HouseWaybill.problem_note ==='))
        hits = self._scan_hawb_field('problem_note')
        self._report_hawb(hits, limit)

        self.stdout.write('')
        self.stdout.write(self.style.NOTICE(
            '=== HouseWaybill.tsd_number с форматом ДТ (возможно перепутали) ==='))
        for h in HouseWaybill.objects.exclude(tsd_number='').iterator():
            if DECL_RE.search(h.tsd_number or ''):
                marker = ' [уже есть ДТ]' if h.customs_declaration_number else ''
                self.stdout.write(f'  HAWB {h.hawb_number}: tsd={h.tsd_number!r}{marker}')

        # ── Cargo ────────────────────────────────────────────────
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE('=== Cargo.description ==='))
        hits = self._scan_cargo_field('description')
        self._report_cargo(hits, limit)

        self.stdout.write('')
        self.stdout.write(self.style.NOTICE('=== Cargo.description_ru ==='))
        hits = self._scan_cargo_field('description_ru')
        self._report_cargo(hits, limit)

        self.stdout.write('')
        self.stdout.write(self.style.NOTICE(
            '=== Cargo.warehouse_name содержит лицензию (вдруг попало туда) ==='))
        for c in Cargo.objects.exclude(warehouse_name='').iterator():
            if LICENSE_RE.search(c.warehouse_name or ''):
                marker = ' [уже есть лицензия]' if c.warehouse_license else ''
                self.stdout.write(
                    f'  Cargo {c.awb_number}: warehouse_name={c.warehouse_name!r}{marker}'
                )

        # ── Сводка ──────────────────────────────────────────────
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=== Сводка по HAWB-полям ==='))
        for field in ('notes', 'problem_note'):
            counts = defaultdict(int)
            for h in HouseWaybill.objects.exclude(**{field: ''}).iterator():
                for pat in scan_text(getattr(h, field)):
                    counts[pat] += 1
            if counts:
                self.stdout.write(f'  {field}: ' + ', '.join(
                    f'{k}={v}' for k, v in sorted(counts.items())))
            else:
                self.stdout.write(f'  {field}: чисто')

    def _scan_hawb_field(self, field: str) -> list[tuple[HouseWaybill, dict]]:
        out = []
        qs = HouseWaybill.objects.exclude(**{field: ''}).only(
            'id', 'hawb_number', 'customs_declaration_number',
            'tsd_number', 'mawb_id', field
        )
        for h in qs.iterator():
            found = scan_text(getattr(h, field))
            if found:
                out.append((h, found))
        return out

    def _scan_cargo_field(self, field: str) -> list[tuple[Cargo, dict]]:
        out = []
        qs = Cargo.objects.exclude(**{field: ''}).only(
            'id', 'awb_number', 'warehouse_license',
            'customs_declaration_number', field
        )
        for c in qs.iterator():
            found = scan_text(getattr(c, field))
            if found:
                out.append((c, found))
        return out

    def _report_hawb(self, hits: list, limit: int) -> None:
        if not hits:
            self.stdout.write('  (пусто)')
            return
        for h, found in (hits[:limit] if limit else hits):
            # Подсветка: пустое ли соответствующее поле
            markers = []
            if 'decl' in found and not h.customs_declaration_number:
                markers.append('ДТ-пусто')
            if 'mawb' in found and not h.mawb_id:
                markers.append('MAWB-пусто')
            m = f' [{",".join(markers)}]' if markers else ''
            self.stdout.write(f'  HAWB {h.hawb_number}{m}: {found}')
        if limit and len(hits) > limit:
            self.stdout.write(f'  ... и ещё {len(hits) - limit}')

    def _report_cargo(self, hits: list, limit: int) -> None:
        if not hits:
            self.stdout.write('  (пусто)')
            return
        for c, found in (hits[:limit] if limit else hits):
            markers = []
            if 'license' in found and not c.warehouse_license:
                markers.append('лицензия-пусто')
            if 'decl' in found and not c.customs_declaration_number:
                markers.append('ДТ-пусто')
            m = f' [{",".join(markers)}]' if markers else ''
            self.stdout.write(f'  Cargo {c.awb_number}{m}: {found}')
        if limit and len(hits) > limit:
            self.stdout.write(f'  ... и ещё {len(hits) - limit}')
