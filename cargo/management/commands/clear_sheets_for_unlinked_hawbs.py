"""Очищает стейл-данные в CargoTrack-колонках Sheets для HAWB без mawb_id.

После пересорта в Sheets индексы HAWB сместились, но наши writeback'и
успели записать данные по СТАРЫМ индексам. После последующего import_sheets
связь row_idx ↔ hawb_number поправилась, но осколочные данные в наших
колонках остались на чужих HAWB.

Если у HAWB в БД mawb_id=None — он принципиально НЕ может иметь:
  - лицензия СВХ (берётся из Cargo.warehouse_license)
  - дата регистрации ДО1 (из Cargo.scan_into_bond)
  - рег.№ ДО1 (из Cargo.svh_do1_reg_number)
  - номер партии (это и есть MAWB)
  - svh_do1_sent_at (per-HAWB, но устанавливается только при наличии MAWB
    в ED.DO1 → значит и mawb должен быть)

Эта команда находит такие HAWB, для каждого пишет пустоту в наши
CargoTrack-колонки. После этого Sheets чистый, без следов пересорта.

Запуск:
    uv run python manage.py clear_sheets_for_unlinked_hawbs --dry-run
    uv run python manage.py clear_sheets_for_unlinked_hawbs
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill, ImportedSheetRow


class Command(BaseCommand):
    help = 'Чистит стейл в CargoTrack-колонках Sheets у HAWB без mawb_id'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        # Все HAWB без mawb_id у которых есть row в Sheets «Общее»
        unlinked = list(HouseWaybill.objects.filter(mawb_id__isnull=True))
        self.stdout.write(f'HAWB без mawb_id всего: {len(unlinked)}')
        if not unlinked:
            return

        # Фильтр: только те которые есть в Sheets (иначе писать некуда)
        hawb_nums = [h.hawb_number for h in unlinked if h.hawb_number]
        in_sheets = set(
            ImportedSheetRow.objects.filter(
                source__kind='general',
                hawb_number_norm__in=hawb_nums,
            ).values_list('hawb_number_norm', flat=True)
        )
        affected = [h for h in unlinked if h.hawb_number in in_sheets]
        self.stdout.write(f'Из них в Sheets «Общее»: {len(affected)}')

        if opts['dry_run']:
            self.stdout.write('')
            self.stdout.write('--- DRY RUN, первые 10 ---')
            for h in affected[:10]:
                self.stdout.write(f'  {h.hawb_number}')
            return

        if not affected:
            return

        # Writeback пустых значений во все наши per-HAWB колонки.
        # Каждая batch_write_* функция сама сравнивает текущее значение в
        # Sheets с ожидаемым (для unlinked HAWB ожидаемое='') и пишет
        # только при diff → идемпотентно, не пишет если уже пусто.
        from cargo.services.sheets.writeback import (
            batch_write_svh_do1_sent_for_hawbs,
            batch_write_svh_do1_weight_for_hawbs,
            batch_write_svh_do1_places_for_hawbs,
            batch_write_svh_do2_dates_for_hawbs,
            batch_write_cargo_mawb_for_hawbs,
            batch_write_filed_dates_for_hawbs,
            batch_write_release_dates_for_hawbs,
            batch_write_declarations_for_hawbs,
        )

        self.stdout.write('')
        self.stdout.write(self.style.NOTICE('Sheets writeback (пустые значения)...'))

        ops = [
            ('svh_do1_sent', batch_write_svh_do1_sent_for_hawbs),
            ('svh_do1_weight', batch_write_svh_do1_weight_for_hawbs),
            ('svh_do1_places', batch_write_svh_do1_places_for_hawbs),
            ('svh_do2_date', batch_write_svh_do2_dates_for_hawbs),
            ('cargo_mawb', batch_write_cargo_mawb_for_hawbs),
            ('filed_date', batch_write_filed_dates_for_hawbs),
            ('release_date', batch_write_release_dates_for_hawbs),
            ('declarations', batch_write_declarations_for_hawbs),
        ]
        for label, fn in ops:
            try:
                n = fn(affected)
                self.stdout.write(f'  {label}: {n} cells')
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  {label}: failed: {e}'))

        # Для cargo-level (лицензия СВХ / дата регистрации ДО1 / рег.№ ДО1) —
        # эти поля пишутся через batch_write_svh_for_cargos с привязкой
        # cargo→row_idx. Для unlinked HAWB мы не можем их очистить напрямую,
        # так как функция принимает Cargos, не HAWBs. Делаем это отдельно:
        # для каждого HAWB прямой ws.update_cell с пустыми значениями в 3 колонки.
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE(
            'Очистка cargo-level колонок (лицензия/scan_into_bond/рег.№ ДО1)...'
        ))
        self._clear_cargo_level_columns_for_hawbs(affected)

    def _clear_cargo_level_columns_for_hawbs(self, hawbs):
        """Прямая очистка трёх Cargo-level колонок для конкретных row_idx HAWB.

        batch_write_svh_for_cargos работает только с Cargos. У unlinked HAWB
        нет Cargo, но row_idx есть. Чистим 3 колонки в Sheets вручную через
        batch_update.
        """
        from collections import defaultdict
        from cargo.services.sheets.client import open_worksheet
        from cargo.services.sheets.writeback import (
            CARGOTRACK_SVH_LICENSE_HEADER,
            CARGOTRACK_SVH_DATE_HEADER,
            CARGOTRACK_SVH_DO1_HEADER,
            _col_letter,
            _ensure_named_column,
            _retry_api,
            _chunked_batch_update,
            _filter_inrange_updates,
        )

        # Группируем HAWB-row_idx по SheetSource
        from cargo.models import SheetSource, ImportedSheetRow
        rows = (ImportedSheetRow.objects
                .filter(source__kind='general',
                        hawb_number_norm__in=[h.hawb_number for h in hawbs])
                .select_related('source'))
        by_source: dict = defaultdict(list)
        sources: dict = {}
        seen: set = set()
        for r in rows:
            if r.hawb_number_norm in seen:
                continue
            seen.add(r.hawb_number_norm)
            sources[r.source_id] = r.source
            by_source[r.source_id].append(r.source_row_index)

        for src_id, ridxs in by_source.items():
            source = sources[src_id]
            try:
                ws = _retry_api(open_worksheet, source, label='clear svh open')
                col_lic = _retry_api(_ensure_named_column, ws,
                                     source.header_row, CARGOTRACK_SVH_LICENSE_HEADER,
                                     label='ensure col_lic')
                col_date = _retry_api(_ensure_named_column, ws,
                                      source.header_row, CARGOTRACK_SVH_DATE_HEADER,
                                      label='ensure col_date')
                col_do1 = _retry_api(_ensure_named_column, ws,
                                     source.header_row, CARGOTRACK_SVH_DO1_HEADER,
                                     label='ensure col_do1')
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f'  {source.name}: open/ensure failed: {e}'))
                continue

            # Читаем текущие значения чтобы писать только реально стейл
            try:
                existing_lic = _retry_api(ws.col_values, col_lic, label='read lic')
                existing_date = _retry_api(ws.col_values, col_date, label='read date')
                existing_do1 = _retry_api(ws.col_values, col_do1, label='read do1')
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f'  {source.name}: read failed: {e}'))
                continue

            letter_lic = _col_letter(col_lic)
            letter_date = _col_letter(col_date)
            letter_do1 = _col_letter(col_do1)

            updates = []
            for ridx in ridxs:
                cur_lic = (existing_lic[ridx - 1]
                           if ridx - 1 < len(existing_lic) else '').strip()
                cur_date = (existing_date[ridx - 1]
                            if ridx - 1 < len(existing_date) else '').strip()
                cur_do1 = (existing_do1[ridx - 1]
                           if ridx - 1 < len(existing_do1) else '').strip()
                if cur_lic:
                    updates.append({'range': f'{letter_lic}{ridx}', 'values': [['']]})
                if cur_date:
                    updates.append({'range': f'{letter_date}{ridx}', 'values': [['']]})
                if cur_do1:
                    updates.append({'range': f'{letter_do1}{ridx}', 'values': [['']]})

            if not updates:
                self.stdout.write(f'  {source.name}: уже чисто')
                continue

            updates = _filter_inrange_updates(updates, ws, source.name)
            n = _chunked_batch_update(ws, updates, 'clear stale svh', source.name)
            self.stdout.write(f'  {source.name}: cleared {n} cells')
