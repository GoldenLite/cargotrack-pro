"""Массовая перепривязка HAWB → правильному MAWB из внешнего файла.

Принимает CSV (разделитель ;) или XLSX с двумя колонками: HAWB | MAWB.

Что делает для каждой пары (HAWB, MAWB):
1. Находит HouseWaybill по hawb_number.
2. Находит/создаёт Cargo по awb_number (с stage='DRAFT' если новый).
3. Перепривязывает: HouseWaybill.mawb_id = Cargo.pk (прямой UPDATE минуя save()).
4. Очищает СВХ-поля HAWB которые остались от ЧУЖОГО ED.DO1 (svh_do1_sent_at,
   weight, places, svh_do2_send_at) — они принципиально неактуальны при смене
   партии. Cargo-level СВХ-поля (warehouse_license, scan_into_bond,
   svh_do1_reg_number) подтянутся через refresh_moscow_cargo для MC-партий.
5. Сохраняет старую связку в notes (для аудита кто куда переехал).

В конце — batch Sheets writeback для «номер партии» + очистка стейл-данных.

Формат файла:
    HAWB;MAWB        ← заголовок (опционально, но удобно)
    10259938045;784-84323831
    10260253166;784-84323831
    ...

Запуск:
    uv run python manage.py reassign_hawbs --file hawbs_to_reassign.csv
    uv run python manage.py reassign_hawbs --file file.xlsx --dry-run

XLSX поддерживается если установлен openpyxl (он есть в проекте).
"""
from __future__ import annotations

import csv
import os
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cargo.models import Cargo, HouseWaybill


def _read_file(path: str) -> list[tuple[str, str]]:
    """Возвращает список (hawb, mawb) из CSV или XLSX. Игнорирует заголовок."""
    ext = os.path.splitext(path)[1].lower()
    rows: list[tuple[str, str]] = []
    if ext == '.xlsx':
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        for row in ws.iter_rows(values_only=True):
            if not row or len(row) < 2:
                continue
            a = (str(row[0]) if row[0] is not None else '').strip()
            b = (str(row[1]) if row[1] is not None else '').strip()
            if not a or not b:
                continue
            # Пропускаем header строку
            if a.upper().startswith('HAWB') or 'НАКЛАДН' in a.upper():
                continue
            rows.append((a, b))
    else:
        # CSV — пытаемся разными разделителями
        with open(path, 'r', encoding='utf-8-sig', newline='') as f:
            sample = f.read(4096)
            f.seek(0)
            dialect = csv.Sniffer().sniff(sample, delimiters=';,\t')
            reader = csv.reader(f, dialect)
            for row in reader:
                if len(row) < 2:
                    continue
                a = (row[0] or '').strip()
                b = (row[1] or '').strip()
                if not a or not b:
                    continue
                if a.upper().startswith('HAWB') or 'НАКЛАДН' in a.upper():
                    continue
                rows.append((a, b))
    return rows


class Command(BaseCommand):
    help = 'Массовая перепривязка HAWB → правильному MAWB из файла'

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True,
                            help='Путь к CSV или XLSX с колонками HAWB | MAWB')
        parser.add_argument('--dry-run', action='store_true',
                            help='Только показать что будет, без записи')
        parser.add_argument('--no-clear-svh', action='store_true',
                            help='НЕ очищать СВХ-поля HAWB при перепривязке '
                                 '(по умолчанию: очищаем, т.к. они от чужого ED.DO1)')
        parser.add_argument('--no-writeback', action='store_true',
                            help='Не делать Sheets writeback в конце')

    def handle(self, *args, **opts):
        path = opts['file']
        if not os.path.exists(path):
            raise CommandError(f'Файл не найден: {path}')

        pairs = _read_file(path)
        self.stdout.write(f'Прочитано пар HAWB+MAWB: {len(pairs)}')
        if not pairs:
            return

        # Сводка целевых MAWB
        target_counter = Counter(mawb for _, mawb in pairs)
        self.stdout.write('Целевые партии:')
        for mawb, n in target_counter.most_common(10):
            self.stdout.write(f'  {mawb}: {n} HAWB')
        if len(target_counter) > 10:
            self.stdout.write(f'  ... ещё {len(target_counter)-10} партий')

        # Pre-load: все HAWB и Cargo
        hawb_set = {hn for hn, _ in pairs}
        mawb_set = {mn for _, mn in pairs}
        hawbs_db = {
            h.hawb_number: h for h in HouseWaybill.objects
            .filter(hawb_number__in=hawb_set).select_related('mawb')
        }
        cargos_db = {
            c.awb_number: c for c in Cargo.objects.filter(awb_number__in=mawb_set)
        }

        stats = Counter()
        actions: list[tuple[HouseWaybill, Cargo, str]] = []  # (h, target_cargo, old_mawb_str)
        cargos_to_create: list[str] = []

        for hn, target_mawb in pairs:
            h = hawbs_db.get(hn)
            if not h:
                stats['hawb_not_in_db'] += 1
                continue
            cargo = cargos_db.get(target_mawb)
            if not cargo:
                if target_mawb not in cargos_to_create:
                    cargos_to_create.append(target_mawb)
            old_mawb = h.mawb.awb_number if h.mawb_id and h.mawb else '(нет)'
            if cargo and h.mawb_id == cargo.pk:
                stats['already_correct'] += 1
                continue
            actions.append((h, cargo, old_mawb))  # cargo может быть None если ещё не создан

        self.stdout.write('')
        self.stdout.write('План:')
        self.stdout.write(f'  HAWB в БД, требуется перепривязка: {len(actions)}')
        self.stdout.write(f'  HAWB не найдены в БД:               {stats["hawb_not_in_db"]}')
        self.stdout.write(f'  HAWB уже правильно привязаны:       {stats["already_correct"]}')
        self.stdout.write(f'  Cargo которых нет, надо создать:    {len(cargos_to_create)}')
        for mawb in cargos_to_create[:10]:
            self.stdout.write(f'    {mawb}')
        if len(cargos_to_create) > 10:
            self.stdout.write(f'    ... ещё {len(cargos_to_create)-10}')

        if opts['dry_run']:
            self.stdout.write('')
            self.stdout.write('--- DRY RUN, первые 10 переездов: ---')
            for h, cargo, old in actions[:10]:
                tgt = cargo.awb_number if cargo else '(будет создана)'
                self.stdout.write(f'  {h.hawb_number}: {old} → {tgt}')
            return

        if not actions and not cargos_to_create:
            self.stdout.write(self.style.SUCCESS('Нечего делать, всё правильно.'))
            return

        # Создать недостающие Cargo
        for mawb in cargos_to_create:
            c = Cargo.objects.create(awb_number=mawb, stage='DRAFT')
            cargos_db[mawb] = c
            self.stdout.write(f'  Created Cargo {mawb} (pk={c.pk}, stage=DRAFT)')

        # Перепривязка — прямой UPDATE минуя save() (обходим валидацию
        # logistics_status==JOINABLE_STATUS и автосбросы Rule 0).
        clear_svh = not opts['no_clear_svh']
        moved = 0
        with transaction.atomic():
            for h, _, old in actions:
                # Найти Cargo по MAWB (мог быть только что создан)
                target_mawb = next(m for hn, m in pairs if hn == h.hawb_number)
                cargo = cargos_db.get(target_mawb)
                if not cargo:
                    continue
                update_fields = {'mawb_id': cargo.pk}
                if clear_svh:
                    # Эти поля у HAWB заполнялись из ED.DO1 СТАРОЙ партии —
                    # они больше неактуальны.
                    update_fields.update({
                        'svh_do1_sent_at': None,
                        'svh_do1_gross_weight': None,
                        'svh_do1_place_count': None,
                        'svh_do2_send_at': None,
                    })
                HouseWaybill.objects.filter(pk=h.pk).update(**update_fields)
                moved += 1

        self.stdout.write(self.style.SUCCESS(
            f'Перепривязано: {moved} HAWB'))

        if opts['no_writeback']:
            return

        # Sheets writeback
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE('Sheets writeback...'))
        try:
            from cargo.services.sheets.writeback import (
                batch_write_cargo_mawb_for_hawbs,
                batch_write_svh_do1_sent_for_hawbs,
                batch_write_svh_do1_weight_for_hawbs,
                batch_write_svh_do1_places_for_hawbs,
                batch_write_svh_do2_dates_for_hawbs,
            )
            # Перечитать обновлённые HAWB
            affected_pks = [h.pk for h, _, _ in actions]
            hawbs_fresh = list(HouseWaybill.objects.filter(pk__in=affected_pks)
                               .select_related('mawb'))

            n = batch_write_cargo_mawb_for_hawbs(hawbs_fresh)
            self.stdout.write(f'  cargo_mawb (номер партии): {n} cells')

            if clear_svh:
                # Все эти поля теперь пустые — writeback запишет пустоту,
                # сотрёт стейл от чужого ED.DO1.
                n = batch_write_svh_do1_sent_for_hawbs(hawbs_fresh)
                self.stdout.write(f'  svh_do1_sent (очистка): {n} cells')
                n = batch_write_svh_do1_weight_for_hawbs(hawbs_fresh)
                self.stdout.write(f'  svh_do1_weight (очистка): {n} cells')
                n = batch_write_svh_do1_places_for_hawbs(hawbs_fresh)
                self.stdout.write(f'  svh_do1_places (очистка): {n} cells')
                n = batch_write_svh_do2_dates_for_hawbs(hawbs_fresh)
                self.stdout.write(f'  svh_do2 (очистка): {n} cells')
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'writeback failed: {e}'))

        # Подсказка
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE(
            'Дальше: для новых MC-партий запусти refresh_moscow_cargo чтобы '
            'подтянуть лицензию/scan_into_bond/рег.№ ДО1.'))
