"""Удалить EXPORT-HAWB которые на самом деле импортные.

В прошлой версии match() auto-create НЕ проверял DeclarationKindCode/
CustomsProcedure в raw_xml outbox observation — поэтому импортные HAWB
(ИМ) тоже создались с shipment_type='EXPORT'.

Логика чистки для каждого EXPORT HAWB:
1. Найти все привязанные AltaInboxMessage → initial_envelope → outbox.
2. Парсить raw_xml каждого outbox.
3. Если хоть один outbox подтверждает ЭК → HAWB настоящая, оставляем.
4. Если ни один не подтверждает (raw_xml пуст или customs_procedure/
   declaration_kind не ЭК) → HAWB ошибочная, удаляем + ImportedSheetRow.

Sheets-строки очищаются (`clear_values`), физически не удаляются —
чтобы не смещать row_index у оставшихся.

Запуск:
    uv run python manage.py cleanup_misclassified_exports --dry-run
    uv run python manage.py cleanup_misclassified_exports
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import (
    AltaInboxMessage, AltaOutboxObservation, HouseWaybill,
    ImportedSheetRow,
)


def _is_export_outbox(obs: AltaOutboxObservation) -> bool:
    """True если raw_xml outbox подтверждает ЭК. False если ИМ/пусто/unknown."""
    if not obs:
        return False
    raw_xml = (obs.parsed_meta or {}).get('raw_xml') or ''
    if not raw_xml:
        return False
    from cargo.services.alta.xml_extract import (
        parse_cmn_11335, parse_cmn_11024,
    )
    try:
        if obs.msg_type == 'CMN.11024':
            r = parse_cmn_11024(raw_xml)
            return (r.get('customs_procedure') or '').strip() == 'ЭК'
        if obs.msg_type in ('CMN.11335', 'CMN.11349'):
            r = parse_cmn_11335(raw_xml)
            return (r.get('declaration_kind') or '').strip() == 'ЭК'
    except Exception:
        return False
    return False


class Command(BaseCommand):
    help = 'Чистка ошибочно созданных EXPORT-HAWB'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        from cargo.services.sheets.writeback import (
            _get_export_source, open_worksheet, _retry_api, _col_letter,
            EXPORT_HEADERS_ORDER,
        )

        export_hawbs = list(
            HouseWaybill.objects.filter(shipment_type='EXPORT')
            .select_related('mawb')
        )
        self.stdout.write(f'EXPORT HAWB: {len(export_hawbs)}')

        to_delete: list[HouseWaybill] = []
        keep_count = 0
        for h in export_hawbs:
            # Все inbox-сообщения с initial_envelope → outbox lookup
            init_envelopes = set()
            for m in AltaInboxMessage.objects.filter(hawb=h):
                pm = m.parsed_meta or {}
                ie = (pm.get('initial_envelope') or '').strip()
                if ie:
                    init_envelopes.add(ie)
            outboxes = list(AltaOutboxObservation.objects.filter(
                envelope_id__in=list(init_envelopes),
                msg_type__in=['CMN.11024', 'CMN.11335', 'CMN.11349'],
            )) if init_envelopes else []
            confirmed_ek = any(_is_export_outbox(o) for o in outboxes)
            if confirmed_ek:
                keep_count += 1
            else:
                to_delete.append(h)

        self.stdout.write(
            f'Подтверждённых ЭК: {keep_count}, на удаление: {len(to_delete)}')
        if opts['dry_run']:
            self.stdout.write('\n--- DRY RUN: первые 30 на удаление ---')
            for h in to_delete[:30]:
                self.stdout.write(
                    f'  {h.hawb_number} decl={h.customs_declaration_number} '
                    f'mawb={h.mawb.awb_number if h.mawb_id else "-"}')
            return

        # Sheets clear для удаляемых
        src = _get_export_source()
        clear_ranges: list[str] = []
        row_indexes = []
        if src:
            for h in to_delete:
                row = ImportedSheetRow.objects.filter(
                    source=src, hawb_number_norm__iexact=h.hawb_number).first()
                if row:
                    row_indexes.append(row.source_row_index)
            # clear A:K по строкам
            ncols = len(EXPORT_HEADERS_ORDER)
            last_letter = _col_letter(ncols)
            for r in row_indexes:
                clear_ranges.append(f'A{r}:{last_letter}{r}')

        if clear_ranges:
            try:
                ws = _retry_api(open_worksheet, src, label='cleanup open')
                # batch_clear ограничен количеством — бьём на пачки 200
                CH = 200
                cleared = 0
                for i in range(0, len(clear_ranges), CH):
                    chunk = clear_ranges[i:i + CH]
                    _retry_api(ws.batch_clear, chunk, label='cleanup batch_clear')
                    cleared += len(chunk)
                self.stdout.write(self.style.SUCCESS(
                    f'Sheets: очищено {cleared} строк'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Sheets cleanup failed: {e}'))

        # Удаление в БД
        deleted_hawbs = 0
        deleted_rows = 0
        deleted_cargos = 0
        for h in to_delete:
            # Cargo (если auto-created CDEK-... и нет других HAWB)
            cargo = h.mawb
            ImportedSheetRow.objects.filter(
                hawb_number_norm__iexact=h.hawb_number).delete()
            deleted_rows += 1
            try:
                h.delete()
                deleted_hawbs += 1
            except Exception as e:
                self.stdout.write(f'  cannot delete HAWB {h.hawb_number}: {e}')
            if cargo and (cargo.awb_number or '').startswith('CDEK-'):
                if not cargo.hawbs.exists():
                    try:
                        cargo.delete()
                        deleted_cargos += 1
                    except Exception:
                        pass

        self.stdout.write(self.style.SUCCESS(
            f'\nУдалено: {deleted_hawbs} HAWB, {deleted_rows} ImportedSheetRow, '
            f'{deleted_cargos} Cargo'))
