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

        # Snapshot момента начала прогона — для sync-delete и safety guard.
        # ImportedSheetRow с last_imported_at старше этой метки пропали из Sheets.
        run_start_ts = timezone.now()
        rows_before = ImportedSheetRow.objects.filter(source=self.source).count()
        seen_row_indices: set[int] = set()

        with transaction.atomic():
            for i, raw in enumerate(rows, start=1):
                row_index = raw.pop('_row_index')
                seen_row_indices.add(row_index)
                data = {k: v for k, v in raw.items() if k}
                ch = _content_hash(data)
                self._process_row(row_index, data, ch)
                if i % 500 == 0:
                    logger.info(
                        'Progress %s: %d/%d (new=%d unchanged=%d)',
                        self.source.name, i, len(rows),
                        self.run.rows_new, self.run.rows_unchanged,
                    )

        if not self.dry_run:
            self._mark_duplicates()
            self._sync_delete(run_start_ts, rows_before, len(seen_row_indices))
            if self.auto_promote and self.source.kind == 'general':
                self._auto_promote_orphans()

        self.run.save()

    def _auto_promote_orphans(self) -> None:
        """После дедупа промоутим оставшиеся orphan-строки в HAWB+Cargo.

        Запускается ПОСЛЕ `_mark_duplicates`, поэтому дубли уже помечены
        и не попадают в выборку — никаких UNIQUE-конфликтов на hawb_number.
        """
        from .promote import promote_row
        qs = (ImportedSheetRow.objects
              .filter(source=self.source, match_status='orphan')
              .exclude(hawb_number_norm=''))
        promoted = 0
        errors = 0
        for r in qs:
            try:
                promote_row(r, user=self.user)
                promoted += 1
            except Exception:
                errors += 1
                logger.exception('auto-promote failed for row %s', r.pk)
        if promoted or errors:
            logger.info('auto-promote %s: promoted=%d errors=%d',
                        self.source.name, promoted, errors)

    # ─── дедуп ──

    def _mark_duplicates(self) -> None:
        """Помечает дубли по hawb_number_norm в рамках одного источника.

        Юзер заполняет Sheets вручную и иногда вносит один HAWB несколько раз.
        Без обработки получаются 2-3 ImportedSheetRow на один HAWB и promote
        создаёт дубли HouseWaybill. Логика:

        1. Группируем строки по hawb_number_norm (пустое = пропускаем).
        2. В группе выбираем «победителя» (он сохранит свой match_status):
           - приоритет 1: уже promoted в HAWB (promoted_hawb_id);
           - приоритет 2: matched (есть matched_hawb_id);
           - приоритет 3: orphan с валидным HAWB (имеет match_status='orphan');
           - tie-breaker: максимальный source_row_index (нижняя строка
             в Sheets = обычно свежее, юзеры дописывают снизу).
        3. Остальные строки группы → match_status='duplicate'.

        Победителю если он был 'duplicate' (с прошлого прогона) — восстанавливаем
        исходный match_status (берём из matcher свежий результат).
        """
        from collections import defaultdict
        from .matcher import match_row

        groups: dict[str, list[ImportedSheetRow]] = defaultdict(list)
        qs = (ImportedSheetRow.objects
              .filter(source=self.source)
              .exclude(hawb_number_norm=''))
        for r in qs:
            groups[r.hawb_number_norm].append(r)

        priority = {'promoted': 4, 'matched': 3, 'orphan': 2,
                    'conflict': 1, 'ambiguous': 1, 'unmatched': 0,
                    'ignored': -1, 'duplicate': -2}

        marked_dup = 0
        restored = 0
        for hawb, rows in groups.items():
            if len(rows) < 2:
                # Одиночка — если ранее был помечен duplicate ошибочно,
                # восстанавливаем валидный match_status (rematch).
                only = rows[0]
                if only.match_status == 'duplicate':
                    match_row(only)
                    only.save(update_fields=['match_status', 'matched_hawb',
                                             'matched_cargo', 'diff_summary'])
                    restored += 1
                continue

            # Выбираем победителя
            def _score(r: ImportedSheetRow):
                return (priority.get(r.match_status, 0), r.source_row_index)
            winner = max(rows, key=_score)

            for r in rows:
                if r.pk == winner.pk:
                    if r.match_status == 'duplicate':
                        # Это новый победитель, ранее помеченный duplicate
                        match_row(r)
                        r.save(update_fields=['match_status', 'matched_hawb',
                                              'matched_cargo', 'diff_summary'])
                        restored += 1
                    continue
                if r.match_status != 'duplicate':
                    r.match_status = 'duplicate'
                    r.save(update_fields=['match_status'])
                    marked_dup += 1

        if marked_dup or restored:
            logger.info('Dedup %s: marked %d as duplicate, restored %d',
                        self.source.name, marked_dup, restored)

    # ─── sync-delete ──

    # Защита от случайной потери: если внезапно >10% строк «исчезли» из
    # snapshot — НЕ удаляем, ждём ручного подтверждения. Типичный кейс —
    # Google API вернул частичные данные или юзер случайно фильтровал.
    SYNC_DELETE_SAFETY_THRESHOLD = 0.10

    def _sync_delete(self, run_start_ts, rows_before: int, rows_in_snapshot: int) -> None:
        """Удаляет ImportedSheetRow которых не было в текущем snapshot Sheets.

        Удаляются ТОЛЬКО `ImportedSheetRow`. Связанные `HouseWaybill`/`Cargo`
        НЕ трогаем — даже если строка пропала, промоутнутый HAWB остаётся.

        Safety guard: если >SYNC_DELETE_SAFETY_THRESHOLD строк пропали —
        блокируем удаление, пишем warning, ждём ручного решения.
        """
        # Кандидаты на удаление = всё что было до start_ts, но last_imported_at
        # не обновился (= не виделось в этом прогоне).
        stale = ImportedSheetRow.objects.filter(
            source=self.source,
            last_imported_at__lt=run_start_ts,
        )
        stale_count = stale.count()
        if stale_count == 0:
            return

        # Считаем процент относительно того что БЫЛО до прогона
        ratio = stale_count / max(rows_before, 1)
        if ratio > self.SYNC_DELETE_SAFETY_THRESHOLD:
            warn = (f'sync-delete ABORTED: {stale_count}/{rows_before} '
                    f'({ratio:.1%}) пропали — это >{self.SYNC_DELETE_SAFETY_THRESHOLD:.0%}. '
                    f'Похоже на сбой Sheets API. Если изменения легитимны — '
                    f'удали руками через admin или Django shell.')
            logger.error('%s | %s', self.source.name, warn)
            self.run.error_message = (self.run.error_message or '') + '\n' + warn
            return

        deleted, _ = stale.delete()
        logger.info('sync-delete %s: удалено %d ImportedSheetRow (%.1f%%)',
                    self.source.name, deleted, ratio * 100)

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
            # auto-promote вызывается ПОСЛЕ цикла, в _auto_promote_orphans —
            # иначе дедуп ещё не помечал дубли и promote_row упал бы на UNIQUE.
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
