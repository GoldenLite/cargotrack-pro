r"""Batch-копия данных SQLite → PostgreSQL через ORM (faithful, resumable).

Зачем ORM, а не dumpdata/pgloader: dumpdata на 5.7ГБ = OOM; pgloader даёт
TZ-дрейф и не дружит с Windows. ORM bulk_create делает корректную адаптацию
типов под бэкенд (jsonb, timestamptz, decimal) на каждой строке.

Ключевые свойства:
- Порядок моделей — топологический по FK (цель FK копируется раньше). Внутри
  модели self-FK ловятся отложенными ограничениями Django (FK на PG создаются
  DEFERRABLE INITIALLY DEFERRED → проверка на COMMIT).
- auto_now / auto_now_add ВРЕМЕННО отключаются на время копии, иначе pre_save
  перезатрёт исходные created_at/updated_at текущим временем.
- bulk_create(ignore_conflicts=True) → идемпотентно/resumable (повторный
  прогон пропускает уже вставленные PK). post_save-сигналы НЕ шлются
  (workflow_runner, writeback подавлены by design).
- ContentType и Permission авто-заполняются post_migrate с ДРУГИМИ pk →
  чистим dest перед копией, чтобы pk совпали с источником (иначе FK бьются).
- Стрип raw_xml: у AltaOutboxObservation старше N дней выкидываем
  parsed_meta['raw_xml'] (база переезжает похудевшей). INBOX не трогаем.
- _base_manager (не objects) — на случай кастомных фильтрующих менеджеров.

Использование (на VPS, читаем из VACUUM-копии, пишем в pg):
    manage.py migrate_to_pg --source-sqlite C:\cargotrack\tmp\db_copy.sqlite3 \
        --dest pg --strip-raw-xml-days 30 --reset-sequences
"""
from __future__ import annotations

import copy as _copy
import datetime as _dt
import time

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import OperationalError, ProgrammingError, connections
from django.db.models import Max
from django.utils import timezone

# app_label.ModelName, авто-заполняемые post_migrate — чистим dest перед копией.
AUTO_POPULATED = {'contenttypes.ContentType', 'auth.Permission'}

# Модели с тяжёлым raw_xml/JSON — мелкий чанк (на 4ГБ RAM 20×4МБ ≈ 80МБ).
CHUNK_OVERRIDES = {
    'cargo.AltaInboxMessage': 20,
    'cargo.AltaOutboxObservation': 20,
    'cargo.HawbCustomsRequest': 100,
    'cargo.ImportedSheetRow': 300,
    'cargo.CrmHawbIndex': 300,
}
DEFAULT_CHUNK = 1000

# Модель, у которой стрипаем raw_xml из parsed_meta для старых строк.
STRIP_MODEL = 'cargo.AltaOutboxObservation'

# Дельта-режим: БОЛЬШИЕ чистые append-логи (никогда не мутируют в норме) —
# вставляем только новые строки по pk > max(dest.pk). Всё ОСТАЛЬНОЕ (мелкое,
# потенциально мутабельное: накладные/партии/индексы/конфиг) — полный upsert
# (update_conflicts), чтобы поймать изменённые статусы/выпуски/скрытия.
DELTA_APPEND_ONLY = {
    'cargo.HawbWorkflowEvent',      # ~5.3M событий
    'cargo.AltaInboxMessage',       # raw_xml до 4МБ
    'cargo.AltaOutboxObservation',  # ~274k
    'cargo.AltaOutboxWaybill',      # ~184k денорм
}


def _fk_deps(model, known: set) -> set:
    """Метки моделей, на которые model ссылается FK/O2O (без self)."""
    deps = set()
    for f in model._meta.get_fields():
        if (f.many_to_one or f.one_to_one) and getattr(f, 'concrete', False):
            rel = f.related_model
            if rel is not None:
                lbl = rel._meta.label
                if lbl in known and lbl != model._meta.label:
                    deps.add(lbl)
    return deps


def _topo_sort(models):
    """FK-цели раньше зависимых. Циклы (если есть) — в конец (deferred FK спасут)."""
    known = {m._meta.label for m in models}
    remaining = {m._meta.label: m for m in models}
    placed, result = set(), []
    while remaining:
        progressed = False
        for lbl, m in list(remaining.items()):
            if _fk_deps(m, known) <= placed:
                result.append(m)
                placed.add(lbl)
                del remaining[lbl]
                progressed = True
        if not progressed:
            for lbl, m in remaining.items():
                result.append(m)
            break
    return result


class Command(BaseCommand):
    help = 'Батч-копия данных SQLite → PostgreSQL через ORM (faithful, resumable).'

    def add_arguments(self, parser):
        parser.add_argument('--source-sqlite', default='',
                            help='Путь к SQLite-копии-источнику (иначе алиас default).')
        parser.add_argument('--dest', default='pg', help='Алиас БД-назначения.')
        parser.add_argument('--strip-raw-xml-days', type=int, default=30,
                            help='Выкинуть parsed_meta.raw_xml у AltaOutboxObservation '
                                 'старше N дней (0 = не стрипать).')
        parser.add_argument('--only', default='',
                            help='Только эта модель (app_label.Model), для отладки.')
        parser.add_argument('--reset-sequences', action='store_true',
                            help='В конце сбросить sequence dest на max(pk)+1.')
        parser.add_argument('--no-copy', action='store_true',
                            help='Только parity/reset, без копирования.')
        parser.add_argument('--delta', action='store_true',
                            help='Догнать изменения поверх уже скопированной БД '
                                 '(cutover): большие append-логи — новые строки по '
                                 'pk>max(dest); остальное — полный upsert. НЕ чистит '
                                 'ContentType/Permission.')

    def handle(self, *args, **opts):
        dest = opts['dest']
        if dest not in connections.databases:
            raise CommandError(f'нет алиаса БД «{dest}» (задай POSTGRES_* / DJANGO_DB_ENGINE)')

        # источник: либо явная SQLite-копия (runtime-алиас), либо default
        source = 'default'
        if opts['source_sqlite']:
            d = _copy.deepcopy(connections.databases['default'])
            # чистый sqlite-конфиг: убираем pg-специфику (OPTIONS.connect_timeout,
            # USER/PASSWORD/HOST/PORT), которую sqlite3.connect не переваривает.
            d.update({
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': opts['source_sqlite'],
                'OPTIONS': {}, 'USER': '', 'PASSWORD': '', 'HOST': '', 'PORT': '',
            })
            connections.databases['migsrc'] = d
            source = 'migsrc'
        self.stdout.write(f'source={source} dest={dest}')

        models = list(apps.get_models(include_auto_created=True))
        ordered = _topo_sort(models)
        if opts['only']:
            ordered = [m for m in ordered if m._meta.label == opts['only']]
            if not ordered:
                raise CommandError(f'модель {opts["only"]} не найдена')

        strip_days = opts['strip_raw_xml_days']
        cutoff = timezone.now() - _dt.timedelta(days=strip_days) if strip_days else None

        delta = opts['delta']
        results = []
        if not opts['no_copy']:
            # ContentType/Permission авто-заполняются post_migrate с чужими pk.
            # Чистим их ЕДИНЫМ пред-шагом (TRUNCATE CASCADE), а не инлайн при
            # копии — иначе DELETE ContentType упал бы по FK от ещё не
            # очищенных auth_permission. В delta-режиме НЕ чистим (pg уже верный).
            if not delta:
                self._clear_auto_populated(dest)
            for model in ordered:
                results.append(self._copy_model(model, source, dest, cutoff, delta))

        if opts['reset_sequences']:
            self._reset_sequences(dest)

        # ── сводка parity ──
        self.stdout.write('')
        self.stdout.write('=== PARITY (label: source -> dest) ===')
        mism = skipped = 0
        for label, src_n, dst_n in results:
            if src_n is None:
                skipped += 1
                self.stdout.write(f'  {label}: SKIPPED (no source table)')
                continue
            flag = '' if src_n == dst_n else '  <<< MISMATCH'
            if src_n != dst_n:
                mism += 1
            self.stdout.write(f'  {label}: {src_n} -> {dst_n}{flag}')
        self.stdout.write(
            f'=== models={len(results)} mismatches={mism} skipped={skipped} ===')

    def _copy_model(self, model, source, dest, cutoff, delta=False):
        label = model._meta.label
        # отключаем auto_now/auto_now_add на время копии
        toggled = []
        for f in model._meta.get_fields():
            if getattr(f, 'auto_now', False) or getattr(f, 'auto_now_add', False):
                toggled.append((f, f.auto_now, f.auto_now_add))
                f.auto_now = False
                f.auto_now_add = False
        try:
            mgr_src = model._base_manager.using(source)
            mgr_dst = model._base_manager.using(dest)
            try:
                src_n = mgr_src.count()
            except (OperationalError, ProgrammingError) as e:
                # источник не имеет таблицы (схема-скью) — пропускаем, не роняем
                self.stdout.write(f'  {label}: SKIP (нет таблицы в источнике: {e})')
                return (label, None, None)
            if src_n == 0:
                return (label, 0, mgr_dst.count())

            chunk = CHUNK_OVERRIDES.get(label, DEFAULT_CHUNK)
            strip = (label == STRIP_MODEL and cutoff is not None)

            # режим записи: append (ignore_conflicts) или upsert (update_conflicts)
            append_mode = True
            pk_name = model._meta.pk.name
            upsert_fields = None
            src_qs = mgr_src.order_by('pk')
            if delta:
                if label in DELTA_APPEND_ONLY:
                    # только новые строки: pk > max(dest.pk)
                    max_pk = mgr_dst.aggregate(m=Max('pk'))['m'] or 0
                    src_qs = mgr_src.filter(pk__gt=max_pk).order_by('pk')
                else:
                    # полный upsert — ловит изменённые строки (статусы/выпуски)
                    append_mode = False
                    upsert_fields = [f.name for f in model._meta.concrete_fields
                                     if not f.primary_key]
                    if not upsert_fields:   # таблица из одного pk — только insert
                        append_mode = True

            def _flush(rows):
                if not rows:
                    return
                if append_mode:
                    mgr_dst.bulk_create(rows, ignore_conflicts=True, batch_size=chunk)
                else:
                    mgr_dst.bulk_create(rows, update_conflicts=True,
                                        unique_fields=[pk_name],
                                        update_fields=upsert_fields, batch_size=chunk)

            t0 = time.time()
            copied, buf = 0, []
            for obj in src_qs.iterator(chunk_size=chunk):
                if strip and getattr(obj, 'received_at', None) and obj.received_at < cutoff:
                    pm = obj.parsed_meta
                    if isinstance(pm, dict) and 'raw_xml' in pm:
                        obj.parsed_meta = {k: v for k, v in pm.items() if k != 'raw_xml'}
                buf.append(obj)
                if len(buf) >= chunk:
                    _flush(buf)
                    copied += len(buf)
                    buf = []
            _flush(buf)
            copied += len(buf)

            dst_n = mgr_dst.count()
            dt = time.time() - t0
            mode = ('delta/upsert ' if (delta and not append_mode)
                    else 'delta/append ' if delta else '')
            self.stdout.write(f'  {label}: {mode}proc {copied}/{src_n} -> dest {dst_n} ({dt:.1f}s)')
            return (label, src_n, dst_n)
        finally:
            for f, an, ana in toggled:
                f.auto_now = an
                f.auto_now_add = ana

    def _clear_auto_populated(self, dest):
        """Очистить в dest таблицы, авто-заполняемые post_migrate (ContentType,
        Permission), чтобы pk совпали с источником. На PG — TRUNCATE ... CASCADE
        (мгновенно, снимает зависимые пустые FK); на sqlite (локальный тест) —
        ORM-delete (FK там не форсятся, порядок не важен)."""
        conn = connections[dest]
        tables = []
        for label in AUTO_POPULATED:
            app_label, model_name = label.split('.')
            tables.append(apps.get_model(app_label, model_name)._meta.db_table)
        if not tables:
            return
        if conn.vendor == 'postgresql':
            with conn.cursor() as cur:
                cur.execute('TRUNCATE ' + ', '.join(tables) + ' CASCADE')
        else:
            for label in AUTO_POPULATED:
                apps.get_model(*label.split('.'))._base_manager.using(dest).all().delete()
        self.stdout.write(f'cleared auto-populated in {dest}: {", ".join(tables)}')

    def _reset_sequences(self, dest):
        style = no_style()
        conn = connections[dest]
        n = 0
        with conn.cursor() as cur:
            for app_config in apps.get_app_configs():
                app_models = list(app_config.get_models(include_auto_created=True))
                for sql in conn.ops.sequence_reset_sql(style, app_models):
                    cur.execute(sql)
                    n += 1
        self.stdout.write(f'sequences reset: {n} statements on {dest}')
