"""Оркестратор импорта одной вкладки Sheets."""
from __future__ import annotations

import hashlib
import json
import logging
import traceback
from typing import Optional

from django.contrib.auth.models import User
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
                 user: Optional[User] = None, verbose: bool = False):
        self.source  = source
        self.dry_run = dry_run
        self.user    = user
        self.verbose = verbose
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

        for raw in rows:
            row_index = raw.pop('_row_index')
            data = {k: v for k, v in raw.items() if k}
            ch = _content_hash(data)
            self._process_row(row_index, data, ch)

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

        obj, created = ImportedSheetRow.objects.update_or_create(
            source=self.source,
            source_row_index=row_index,
            defaults={
                'data': data,
                'content_hash': ch,
                'last_imported_at': timezone.now(),
            },
        )
        changed = created or (obj.content_hash != ch and not created)
        # update_or_create уже выставил content_hash в defaults, поэтому detection
        # делаем по флагу created + наличие previous-hash. Здесь упростим:
        # если запись свежая или была only что обновлена — считаем changed.

        # Re-fetch чтобы получить «было до» — но это лишний запрос. Делаем проще:
        # хеш всегда перезаписывается; запускаем match только если created OR
        # это первый прогон (match_status == 'unmatched').
        do_match = created or obj.match_status == 'unmatched' or obj.diff_summary == {}
        if do_match:
            match_row(obj)
            obj.save()
            if self.source.kind == 'crm' and obj.matched_hawb_id:
                emit_workflow_events(obj)

        self._tick_counters(obj, created=created, changed=changed)
        if self.verbose:
            logger.info('#%s: %s %s', row_index, 'NEW' if created else 'UPD', obj.match_status)

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
