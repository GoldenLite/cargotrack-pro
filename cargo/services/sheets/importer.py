"""Оркестратор импорта одной вкладки Sheets."""
from __future__ import annotations

import hashlib
import json
import logging
import traceback
from typing import Optional

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from cargo.models import ImportedSheetRow, SheetImportRun, SheetSource

from .client import SheetsConfigError, open_worksheet
from .events import emit_workflow_events
from .matcher import match_row


logger = logging.getLogger('cargo.sheets')


def _content_hash(data: dict) -> str:
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _read_rows(worksheet, header_row: int) -> list[dict]:
    """Читает все строки и формирует [{header: value, ...}, ...]. Шапка пропускается."""
    all_values = worksheet.get_all_values()
    if len(all_values) < header_row:
        return []
    header = [h.strip() for h in all_values[header_row - 1]]
    data_rows = []
    for idx, values in enumerate(all_values[header_row:], start=header_row + 1):
        # выравниваем длину
        padded = values + [''] * (len(header) - len(values))
        row = {h: padded[i].strip() for i, h in enumerate(header) if h}
        if not any(row.values()):
            continue  # пустая строка
        data_rows.append({'_row_index': idx, **row})
    return data_rows


class SheetImporter:
    """Один прогон импорта по конкретному SheetSource."""

    def __init__(self, source: SheetSource, *, dry_run: bool = False,
                 user: Optional[User] = None, verbose: bool = False,
                 auto_promote: bool = False):
        self.source  = source
        self.dry_run = dry_run
        self.user    = user
        self.verbose = verbose
        # auto_promote: для kind='general' каждый свежесозданный orphan
        # сразу прогоняется через promote_row → HAWB + Cargo + relink.
        self.auto_promote = auto_promote
        self.run: Optional[SheetImportRun] = None

    def run_once(self) -> SheetImportRun:
        self.run = SheetImportRun.objects.create(
            source=self.source,
            triggered_by=self.user,
            dry_run=self.dry_run,
        )
        try:
            self._do_run()
            self.run.status = 'ok'
            self.source.last_status = 'ok'
            self.source.last_error  = ''
        except SheetsConfigError as e:
            self.run.status = 'error'
            self.run.error_message = str(e)
            self.source.last_status = 'error'
            self.source.last_error  = str(e)[:5000]
            logger.error('Sheets config error for %s: %s', self.source, e)
        except Exception as e:
            tb = traceback.format_exc()
            self.run.status = 'error'
            self.run.error_message = tb
            self.source.last_status = 'error'
            self.source.last_error  = str(e)[:5000]
            logger.exception('Sheet import crashed for %s', self.source)
        finally:
            self.run.finished_at = timezone.now()
            self.run.save()
            self.source.last_imported_at = self.run.finished_at
            self.source.save(update_fields=['last_imported_at', 'last_status', 'last_error'])
        return self.run

    def _do_run(self) -> None:
        ws = open_worksheet(self.source)
        rows = _read_rows(ws, self.source.header_row)
        self.run.rows_total = len(rows)
        logger.info('Importing %s: %d data rows', self.source.name, len(rows))

        with transaction.atomic():
            for i, raw in enumerate(rows, start=1):
                row_index = raw.pop('_row_index')
                data = {k: v for k, v in raw.items() if k}
                ch = _content_hash(data)
                self._process_row(row_index, data, ch)
                if i % 500 == 0:
                    logger.info(
                        'Progress %s: %d/%d (new=%d unchanged=%d)',
                        self.source.name, i, len(rows),
                        self.run.rows_new, self.run.rows_unchanged,
                    )

        self.run.save()

    def _process_row(self, row_index: int, data: dict, ch: str) -> None:
        if self.dry_run:
            obj = ImportedSheetRow(
                source=self.source, source_row_index=row_index,
                data=data, content_hash=ch,
            )
            # in-memory matching для dry-run счётчиков (без save)
            from .matcher import match_row as _m
            _m(obj)
            self._tick_counters(obj, created=True, changed=True)
            if self.verbose:
                logger.info('DRY #%s: %s', row_index, obj.match_status)
            return

        existing = (
            ImportedSheetRow.objects
            .filter(source=self.source, source_row_index=row_index)
            .only('id', 'content_hash', 'match_status')
            .first()
        )
        now = timezone.now()

        if existing is None:
            obj = ImportedSheetRow.objects.create(
                source=self.source,
                source_row_index=row_index,
                data=data,
                content_hash=ch,
                last_imported_at=now,
                first_seen_at=now,
                last_seen_at=now,
            )
            match_row(obj)
            obj.save()
            if self.source.kind == 'crm' and obj.matched_hawb_id:
                emit_workflow_events(obj)
            # Auto-promote: orphan-ряд из «Общее» сразу превращаем в HAWB +
            # автосоздание Cargo. Включается флагом --auto-promote в CLI.
            if (self.auto_promote
                    and self.source.kind == 'general'
                    and obj.match_status == 'orphan'
                    and obj.hawb_number_norm):
                try:
                    from .promote import promote_row
                    promote_row(obj, user=self.user)
                except Exception:
                    logger.exception('auto-promote failed for row %s', obj.pk)
            self._tick_counters(obj, created=True, changed=True)
            if self.verbose:
                logger.info('#%s: NEW %s', row_index, obj.match_status)
            return

        if existing.content_hash == ch:
            ImportedSheetRow.objects.filter(pk=existing.pk).update(
                last_imported_at=now, last_seen_at=now,
            )
            existing.match_status = existing.match_status or 'unmatched'
            self._tick_counters(existing, created=False, changed=False)
            return

        existing.data = data
        existing.content_hash = ch
        existing.last_imported_at = now
        existing.last_seen_at = now
        match_row(existing)
        existing.save()
        if self.source.kind == 'crm' and existing.matched_hawb_id:
            emit_workflow_events(existing)
        self._tick_counters(existing, created=False, changed=True)
        if self.verbose:
            logger.info('#%s: UPD %s', row_index, existing.match_status)

    def _tick_counters(self, obj: ImportedSheetRow, *, created: bool, changed: bool) -> None:
        if created:
            self.run.rows_new += 1
        elif changed:
            self.run.rows_changed += 1
        else:
            self.run.rows_unchanged += 1
        if obj.match_status == 'matched':
            self.run.rows_matched += 1
        elif obj.match_status == 'orphan':
            self.run.rows_orphan += 1
        elif obj.match_status == 'conflict':
            self.run.rows_conflict += 1
