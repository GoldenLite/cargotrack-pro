"""Вытащить рег.номер ДТ и лицензию СВХ из HAWB.notes в реальные поля.

При promote из Sheets подсказки СВХ и ДТ дублируются в notes (потому что
HouseWaybill.save() автоматически очищает customs_declaration_number если
нет mawb или checklist неполный). Эта команда делает обратное действие
для уже промоутнутых HAWB:

- HAWB.notes содержит «Рег. номер ДТ из Sheets: NUMBER» →
  HouseWaybill.customs_declaration_number ← NUMBER
  (записывается через .update(), минуя save()-clear);
- HAWB.notes содержит «СВХ из Sheets: 10001/060324/10009/1 (...)» →
  Cargo(=mawb).warehouse_license ← 10001/060324/10009/1
  (только если у HAWB есть mawb);

Правило: НЕ перезаписываем. Заполняем только если соответствующее поле
пустое. Триггерит писать обновлённые ячейки в Sheets.

Запуск:
    python manage.py extract_notes_to_fields --dry-run
    python manage.py extract_notes_to_fields
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand

from cargo.models import Cargo, HouseWaybill


_DECL_RE = re.compile(r'Рег\.\s*номер\s+ДТ\s+из\s+Sheets:\s*(\S+)', re.I)
# СВХ запись формата «10001/060324/10009/1 (текст в скобках)» —
# берём всё до первого пробела/открывающей скобки.
_LIC_RE  = re.compile(r'СВХ\s+из\s+Sheets:\s*([\d/]+)', re.I)

# Базовая проверка валидности (просто эвристика, не строгая).
_DECL_OK_RE = re.compile(r'^\d{6,}/\d{6}/\d{5,}$')
_LIC_OK_RE  = re.compile(r'^\d{4,}/\d{6}/\d{4,}/\d+$')


class Command(BaseCommand):
    help = 'Извлечь ДТ и лицензию СВХ из HAWB.notes в customs_declaration_number и Cargo.warehouse_license'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        candidates = HouseWaybill.objects.exclude(notes='').only(
            'pk', 'hawb_number', 'notes', 'customs_declaration_number',
            'mawb_id', 'filed_date',
        )
        total = candidates.count()
        self.stdout.write(f'HAWB с заметками: {total}')

        # Соберём по партиям, чтобы не дёргать одну и ту же Cargo несколько раз
        cargo_lic_updates: dict[int, tuple[str, set[str]]] = {}
        # cargo_pk → (license, set_of_hawb_numbers_for_audit)

        hawb_decl_updates: list[tuple[HouseWaybill, str]] = []
        skipped_invalid_decl  = 0
        skipped_invalid_lic   = 0
        skipped_decl_already  = 0
        skipped_lic_already   = 0
        no_mawb_for_lic       = 0

        for h in candidates.iterator():
            text = h.notes or ''
            # ── ДТ ──
            m = _DECL_RE.search(text)
            if m:
                decl = m.group(1).strip().rstrip('.,;:)')
                if not _DECL_OK_RE.match(decl):
                    skipped_invalid_decl += 1
                elif (h.customs_declaration_number or '').strip():
                    skipped_decl_already += 1
                else:
                    hawb_decl_updates.append((h, decl))

            # ── СВХ ──
            m = _LIC_RE.search(text)
            if m:
                lic = m.group(1).strip().rstrip('/')
                if not _LIC_OK_RE.match(lic):
                    skipped_invalid_lic += 1
                elif not h.mawb_id:
                    no_mawb_for_lic += 1
                else:
                    # Группируем по Cargo
                    if h.mawb_id in cargo_lic_updates:
                        existing_lic, hset = cargo_lic_updates[h.mawb_id]
                        hset.add(h.hawb_number)
                        if existing_lic != lic:
                            self.stdout.write(self.style.WARNING(
                                f'  CONFLICT: cargo_id={h.mawb_id} '
                                f'lic={existing_lic!r} vs {lic!r} '
                                f'(HAWB {h.hawb_number})'))
                    else:
                        cargo_lic_updates[h.mawb_id] = (lic, {h.hawb_number})

        # Отфильтруем те Cargo у которых warehouse_license уже стоит
        cargos = {c.pk: c for c in Cargo.objects.filter(pk__in=cargo_lic_updates)
                  .only('pk', 'awb_number', 'warehouse_license')}
        cargo_to_write: list[tuple[Cargo, str, int]] = []
        for cargo_pk, (lic, hset) in cargo_lic_updates.items():
            c = cargos.get(cargo_pk)
            if not c:
                continue
            if (c.warehouse_license or '').strip():
                skipped_lic_already += 1
                continue
            cargo_to_write.append((c, lic, len(hset)))

        self.stdout.write('\n──── ИТОГИ АНАЛИЗА ────')
        self.stdout.write(f'ДТ можно вписать:        {len(hawb_decl_updates)}')
        self.stdout.write(f'  пропущено (уже стоит): {skipped_decl_already}')
        self.stdout.write(f'  пропущено (мусор):     {skipped_invalid_decl}')
        self.stdout.write(f'СВХ-лицензий вписать:    {len(cargo_to_write)} '
                          f'(касается {sum(n for _,_,n in cargo_to_write)} HAWB)')
        self.stdout.write(f'  пропущено (уже стоит): {skipped_lic_already}')
        self.stdout.write(f'  пропущено (мусор):     {skipped_invalid_lic}')
        self.stdout.write(f'  пропущено (нет mawb):  {no_mawb_for_lic}')

        # Превью
        self.stdout.write('\n──── ПРИМЕРЫ ДТ ────')
        for h, d in hawb_decl_updates[:10]:
            self.stdout.write(f'  HAWB {h.hawb_number}: → {d}')
        if len(hawb_decl_updates) > 10:
            self.stdout.write(f'  ... ещё {len(hawb_decl_updates)-10}')

        self.stdout.write('\n──── ПРИМЕРЫ СВХ ────')
        for c, lic, n in cargo_to_write[:10]:
            self.stdout.write(f'  Cargo {c.awb_number}: → {lic} ({n} HAWB)')
        if len(cargo_to_write) > 10:
            self.stdout.write(f'  ... ещё {len(cargo_to_write)-10}')

        if opts['dry_run']:
            return

        # Применяем
        if hawb_decl_updates:
            for h, decl in hawb_decl_updates:
                HouseWaybill.objects.filter(pk=h.pk).update(
                    customs_declaration_number=decl)
            self.stdout.write(self.style.SUCCESS(
                f'\nUpdated {len(hawb_decl_updates)} HAWB.customs_declaration_number'))

        if cargo_to_write:
            affected_cargos: list[Cargo] = []
            for c, lic, _ in cargo_to_write:
                # На Cargo нет автоклира warehouse_license — можно save(),
                # но для скорости через .update().
                Cargo.objects.filter(pk=c.pk).update(warehouse_license=lic)
                c.warehouse_license = lic
                affected_cargos.append(c)
            self.stdout.write(self.style.SUCCESS(
                f'Updated {len(affected_cargos)} Cargo.warehouse_license'))

        # Writeback в Sheets:
        # - ДТ пишем батчем (1 запрос на все)
        # - Лицензия СВХ — НЕ дёргаем per-cargo writeback (he 469x open вылетает
        #   в 429 quota). Полагаемся на audit_sheets_vs_db --fix из auto_sync —
        #   он делает 1 read + 1 batch_update со всеми изменениями.
        try:
            from cargo.services.sheets.writeback import (
                batch_write_declarations_for_hawbs,
            )
            if hawb_decl_updates:
                hawbs = [h for h, _ in hawb_decl_updates]
                for h in hawbs:
                    h.refresh_from_db(fields=['customs_declaration_number'])
                n = batch_write_declarations_for_hawbs(hawbs)
                self.stdout.write(f'Sheets: ДТ-cells обновлено {n}')
            if cargo_to_write:
                self.stdout.write(
                    'Sheets: СВХ-лицензии будут протянуты ближайшим '
                    'audit_sheets_vs_db --fix (auto_sync через ~30 мин). '
                    'Можно дёрнуть вручную: '
                    'manage.py audit_sheets_vs_db --fix')
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'writeback failed: {e}'))
