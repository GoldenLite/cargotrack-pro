"""Безопасный backfill архива Alta inbox-сообщений в AltaInboxMessage.

Архив — gzip NDJSON, по одной записи на строку:
    {"env":"...", "type":"CMN.11337", "dt":"2026-...", "zip":1, "b64":"..."}
где b64 = base64(zlib.compress(xml_text)).

Workflow:
1. Safety check на env SHEETS_WRITEBACK_DISABLED / ALTA_INBOX_AUTOCREATE_DISABLED
   чтобы случайно не дёрнуть писалку в Sheets / auto-create HAWB-веток.
2. Загрузка существующих envelope_id в memory-set для dedup.
3. Стрим NDJSON → фильтры (start-date, types) → агрегаты или bulk_create.
4. В real-run dispatch() вызывается синхронно для каждой реально созданной
   записи (если не --no-dispatch). Snapshots SQLite каждые 5000 applied.

Запуск (типовой):
    set SHEETS_WRITEBACK_DISABLED=1
    set ALTA_INBOX_AUTOCREATE_DISABLED=1
    uv run python manage.py backfill_alta_inbox \\
        --archive d:/cargotrack_pro/_alta_backfill/alta_msgs.ndjson.gz --dry-run
"""
from __future__ import annotations

import base64
import gzip
import json
import logging
import os
import shutil
import time
import zlib
from collections import Counter
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.dateparse import parse_datetime

from cargo.models import AltaInboxMessage
from cargo.services.alta.inbox import classify, dispatch
from cargo.services.alta.xml_extract import parse_raw_xml


logger = logging.getLogger('cargo.alta.backfill')


def _env_disabled(name: str) -> bool:
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


def _parse_date_only(s: str):
    """YYYY-MM-DD → date или None."""
    if not s:
        return None
    try:
        from datetime import date
        y, m, d = s.split('-')
        return date(int(y), int(m), int(d))
    except Exception:
        return None


def _safe_parse_dt(s: str):
    if not s:
        return None
    try:
        return parse_datetime(s)
    except Exception:
        return None


class Command(BaseCommand):
    help = (
        'Безопасный backfill архива Alta inbox-сообщений (gzip NDJSON) '
        'в таблицу AltaInboxMessage. По умолчанию dispatch() вызывается '
        'синхронно. Используй --dry-run для агрегатов или --no-dispatch '
        'чтобы только залить bulk_create без побочных эффектов.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--archive', required=True,
                            help='Путь к gzip NDJSON архиву')
        parser.add_argument('--dry-run', action='store_true',
                            help='Только агрегаты, никакого bulk_create')
        parser.add_argument('--no-dispatch', action='store_true',
                            help='Только bulk_create, без dispatch '
                                 '(полезно для legacy ED.11002 / refill)')
        parser.add_argument('--batch-size', type=int, default=300,
                            help='Размер батча для bulk_create (default 300)')
        parser.add_argument('--sleep', type=float, default=0.2,
                            help='Sleep между батчами в секундах (default 0.2)')
        parser.add_argument('--start-date',
                            help='Фильтр: dt >= YYYY-MM-DD (опционально)')
        parser.add_argument('--types',
                            help='CSV msg_type фильтр, например '
                                 '"CMN.11337,CMN.11350" (опционально)')
        parser.add_argument('--limit', type=int,
                            help='Обработать первые N после фильтров')
        parser.add_argument('--force', action='store_true',
                            help='Пропустить env-safety-check '
                                 '(НЕ рекомендуется на проде)')

    # ──────────────────────────────────────────────────────────────────
    def handle(self, *args, **opts):
        archive_path = Path(opts['archive'])
        dry_run = opts['dry_run']
        no_dispatch = opts['no_dispatch']
        batch_size = max(1, int(opts['batch_size']))
        sleep_sec = max(0.0, float(opts['sleep']))
        start_date = _parse_date_only(opts.get('start_date') or '')
        types_csv = (opts.get('types') or '').strip()
        types_filter = {t.strip() for t in types_csv.split(',') if t.strip()} \
            if types_csv else None
        limit_n = opts.get('limit')
        force = opts['force']

        # ── 1. Safety check ──
        if not force:
            sheets_off = _env_disabled('SHEETS_WRITEBACK_DISABLED')
            autocreate_off = _env_disabled('ALTA_INBOX_AUTOCREATE_DISABLED')
            if not (sheets_off and autocreate_off):
                self.stdout.write(self.style.WARNING(
                    'WARNING: env-safety-check failed.\n'
                    '  SHEETS_WRITEBACK_DISABLED = '
                    f'{os.environ.get("SHEETS_WRITEBACK_DISABLED", "")!r}\n'
                    '  ALTA_INBOX_AUTOCREATE_DISABLED = '
                    f'{os.environ.get("ALTA_INBOX_AUTOCREATE_DISABLED", "")!r}\n'
                    'Сначала выставь env SHEETS_WRITEBACK_DISABLED=1 и '
                    'ALTA_INBOX_AUTOCREATE_DISABLED=1, либо передай --force '
                    'чтобы продолжить без них (НЕ рекомендуется на проде).'
                ))
                return

        if not archive_path.exists():
            self.stdout.write(self.style.ERROR(
                f'Archive not found: {archive_path}'))
            return

        t_start = time.time()
        self.stdout.write(f'Archive: {archive_path}')
        self.stdout.write(
            f'Mode: dry_run={dry_run}, no_dispatch={no_dispatch}, '
            f'batch_size={batch_size}, sleep={sleep_sec}')
        if start_date:
            self.stdout.write(f'  start_date >= {start_date}')
        if types_filter:
            self.stdout.write(f'  types filter: {sorted(types_filter)}')
        if limit_n:
            self.stdout.write(f'  limit: {limit_n}')

        # ── 2. dedup-set ──
        self.stdout.write('Loading existing envelope_id set…')
        seen_set = {
            (v or '').lower()
            for v in AltaInboxMessage.objects.values_list('envelope_id',
                                                          flat=True)
        }
        self.stdout.write(f'  existing envelopes in DB: {len(seen_set)}')

        # ── 3. Стрим архива → pending ──
        total_archive = 0
        total_after_filter = 0
        skipped_dedup = 0
        skipped_bad_json = 0
        pending: list[dict] = []

        self.stdout.write('Reading archive…')
        with gzip.open(archive_path, 'rb') as fh:
            for raw_line in fh:
                total_archive += 1
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    skipped_bad_json += 1
                    continue

                env_orig = (obj.get('env') or '').strip()
                env_lower = env_orig.lower()
                msg_type = (obj.get('type') or '').strip()
                dt_str = obj.get('dt') or ''
                b64 = obj.get('b64') or ''

                if not env_orig or not b64:
                    skipped_bad_json += 1
                    continue

                # фильтры
                if types_filter and msg_type not in types_filter:
                    continue
                if start_date:
                    dt_obj = _safe_parse_dt(dt_str)
                    if dt_obj and dt_obj.date() < start_date:
                        continue
                    if dt_obj is None:
                        # без даты — отбрасываем при включённом start-date
                        continue

                total_after_filter += 1

                if env_lower in seen_set:
                    skipped_dedup += 1
                    continue

                pending.append({
                    'env': env_orig,
                    'type': msg_type,
                    'dt': dt_str,
                    'b64': b64,
                })

        # ── 4. сортировка по dt ASC, None в конец ──
        def _sort_key(item):
            d = _safe_parse_dt(item.get('dt') or '')
            return (0, d) if d is not None else (1, None)
        pending.sort(key=_sort_key)

        # ── 5. limit ──
        if limit_n and len(pending) > limit_n:
            pending = pending[:limit_n]

        total_pending = len(pending)
        self.stdout.write(self.style.SUCCESS(
            f'Scan complete: archive_lines={total_archive}, '
            f'after_filter={total_after_filter}, '
            f'dedup_skip={skipped_dedup}, bad_json={skipped_bad_json}, '
            f'pending={total_pending}'))

        # ── 6. Dry-run ──
        if dry_run:
            by_type = Counter()
            by_month = Counter()
            kind_estimate = Counter()
            samples = []
            for it in pending:
                by_type[it['type']] += 1
                d = _safe_parse_dt(it.get('dt') or '')
                if d is not None:
                    by_month[f'{d.year:04d}-{d.month:02d}'] += 1
                else:
                    by_month['UNKNOWN'] += 1
                try:
                    kind_estimate[classify(it['type'], None)] += 1
                except Exception:
                    kind_estimate['ERROR'] += 1
                if len(samples) < 20:
                    samples.append({'env': it['env'], 'type': it['type'],
                                    'dt': it['dt']})

            report = {
                'archive': str(archive_path),
                'total_archive_lines': total_archive,
                'skipped_bad_json': skipped_bad_json,
                'total_after_filter': total_after_filter,
                'skipped_dedup': skipped_dedup,
                'total_pending': total_pending,
                'by_type_top15': by_type.most_common(15),
                'by_month': sorted(by_month.items()),
                'kind_estimate': sorted(kind_estimate.items(),
                                        key=lambda kv: -kv[1]),
                'samples': samples,
                'elapsed_sec': round(time.time() - t_start, 2),
            }
            self.stdout.write(json.dumps(report, ensure_ascii=False,
                                         indent=2, default=str))
            out_path = archive_path.parent / 'backfill_dry_run_report.json'
            try:
                out_path.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2,
                               default=str),
                    encoding='utf-8')
                self.stdout.write(self.style.SUCCESS(
                    f'Dry-run report saved: {out_path}'))
            except Exception as e:
                self.stdout.write(self.style.WARNING(
                    f'Could not save dry-run report: {e}'))
            return

        # ── 7. Real run ──
        if not pending:
            self.stdout.write(self.style.SUCCESS('Nothing to apply, exiting.'))
            return

        applied_count = 0
        error_count = 0
        errors_in_row = 0
        dispatched_count = 0
        snapshot_idx = 0

        i = 0
        while i < total_pending:
            batch_slice = pending[i:i + batch_size]
            i += batch_size

            batch_objs = []
            for it in batch_slice:
                env_orig = it['env']
                msg_type = it['type']
                dt_str = it.get('dt') or ''
                b64 = it['b64']
                try:
                    xml_str = zlib.decompress(base64.b64decode(b64)).decode(
                        'utf-8', errors='replace')
                except Exception as e:
                    logger.warning('decompress failed env=%s: %s', env_orig, e)
                    error_count += 1
                    errors_in_row += 1
                    if errors_in_row > 100:
                        self.stdout.write(self.style.ERROR(
                            '100 errors in row, aborting; resume by '
                            're-running command.'))
                        return
                    continue

                try:
                    parsed_meta = parse_raw_xml(xml_str) or {}
                except Exception as e:
                    logger.warning('parse_raw_xml failed env=%s: %s',
                                   env_orig, e)
                    parsed_meta = {'parse_error': str(e)}

                prepared_at = _safe_parse_dt(
                    parsed_meta.get('prepared_at') or '') \
                    or _safe_parse_dt(dt_str)

                batch_objs.append(AltaInboxMessage(
                    envelope_id=env_orig,
                    msg_type=(msg_type or '')[:32],
                    prepared_at=prepared_at,
                    raw_xml=xml_str,
                    parsed_meta=parsed_meta,
                ))
                errors_in_row = 0  # сбросили на успешном prep

            if not batch_objs:
                continue

            env_ids_in_batch = [o.envelope_id for o in batch_objs]
            try:
                with transaction.atomic():
                    AltaInboxMessage.objects.bulk_create(
                        batch_objs, ignore_conflicts=True)
                    # Какие реально созданы — те у которых раньше не было
                    # envelope_id в БД. Перечитываем через SELECT.
                    created_qs = AltaInboxMessage.objects.filter(
                        envelope_id__in=env_ids_in_batch)
                    created_by_env = {m.envelope_id: m for m in created_qs}
            except Exception as e:
                logger.exception('bulk_create failed: %s', e)
                error_count += len(batch_objs)
                errors_in_row += len(batch_objs)
                if errors_in_row > 100:
                    self.stdout.write(self.style.ERROR(
                        '100 errors in row, aborting; resume by '
                        're-running command.'))
                    return
                continue

            # Считаем как applied те, чьих env_id раньше не было в seen_set.
            newly_applied = []
            for o in batch_objs:
                if o.envelope_id.lower() not in seen_set:
                    newly_applied.append(created_by_env.get(o.envelope_id))
                    seen_set.add(o.envelope_id.lower())
            newly_applied = [m for m in newly_applied if m is not None]

            applied_count += len(newly_applied)

            # ── dispatch ──
            if not no_dispatch:
                for m in newly_applied:
                    try:
                        dispatch(m)
                        dispatched_count += 1
                        errors_in_row = 0
                    except Exception as e:
                        logger.exception('dispatch failed env=%s: %s',
                                         m.envelope_id, e)
                        error_count += 1
                        errors_in_row += 1
                        if errors_in_row > 100:
                            self.stdout.write(self.style.ERROR(
                                '100 errors in row, aborting; resume by '
                                're-running command.'))
                            return

            # ── snapshot SQLite каждые 5000 applied ──
            if applied_count and applied_count // 5000 > snapshot_idx:
                snapshot_idx = applied_count // 5000
                try:
                    db_path = settings.DATABASES['default']['NAME']
                    snap_path = f'{db_path}.snap.{snapshot_idx:03d}.bak'
                    shutil.copy(db_path, snap_path)
                    self.stdout.write(self.style.SUCCESS(
                        f'snapshot saved: {snap_path}'))
                except Exception as e:
                    logger.warning('snapshot failed: %s', e)
                    self.stdout.write(self.style.WARNING(
                        f'snapshot failed: {e}'))

            # ── progress log ──
            done = min(i, total_pending)
            if done % 500 < batch_size or done >= total_pending:
                self.stdout.write(
                    f'progress: {done} / {total_pending}, '
                    f'applied={applied_count}, dispatched={dispatched_count}, '
                    f'dedup_skip={skipped_dedup}, errors={error_count}')

            if sleep_sec > 0 and i < total_pending:
                time.sleep(sleep_sec)

        elapsed = round(time.time() - t_start, 2)
        self.stdout.write(self.style.SUCCESS(
            f'\nDONE. applied={applied_count}, dispatched={dispatched_count}, '
            f'dedup_skip={skipped_dedup}, errors={error_count}, '
            f'elapsed={elapsed}s'))
