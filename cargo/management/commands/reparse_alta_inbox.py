"""Перепарсить parsed_meta из raw_xml для AltaInboxMessage.

Новый парсер (xml_extract.parse_raw_xml) умеет правильно выкусывать
актуальную ДТ когда в XML несколько разных GTDNumber (КДТ-уведомление).
Эта команда применяет его к историческим сообщениям без обновления
агента на work-сервере.

Сохраняем `original_parsed_meta` в случае несовпадения — для аудита.

После reparse делает dispatch(msg), чтобы classify+match+recompute
сработали на свежих данных.

Запуск:
    uv run python manage.py reparse_alta_inbox             # все
    uv run python manage.py reparse_alta_inbox --msg 3345  # один
    uv run python manage.py reparse_alta_inbox --kind released
    uv run python manage.py reparse_alta_inbox --dry-run
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db import connection, OperationalError

from cargo.models import AltaInboxMessage
from cargo.services.alta.inbox import dispatch
from cargo.services.alta.xml_extract import parse_raw_xml


class Command(BaseCommand):
    help = 'Перепарсить parsed_meta из raw_xml + передиспатчить сообщения'

    def add_arguments(self, parser):
        parser.add_argument('--msg', type=int, default=0, help='Конкретный msg id')
        parser.add_argument('--kind', default='', help='Только указанный msg_kind')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--dry-run', action='store_true',
                            help='Только показать diff, без сохранения')
        parser.add_argument('--force-dispatch', action='store_true',
                            help='Вызывать dispatch() даже если parsed_meta не изменился '
                                 '(нужно при обновлении логики в inbox.py — multi-waybill, '
                                 'TZ-fix, любая новая бизнес-логика которая работает поверх '
                                 'уже сохранённых сообщений)')

    def handle(self, *args, **opts):
        if connection.vendor == 'sqlite':
            with connection.cursor() as c:
                c.execute('PRAGMA busy_timeout=60000;')

        qs = AltaInboxMessage.objects.all().order_by('prepared_at')
        if opts['msg']:
            qs = qs.filter(pk=opts['msg'])
        if opts['kind']:
            qs = qs.filter(msg_kind=opts['kind'])
        if opts['limit']:
            qs = qs[:opts['limit']]

        pks = list(qs.values_list('pk', flat=True))
        self.stdout.write(f'Reparse: {len(pks)} messages, dry_run={opts["dry_run"]}')

        # ── Подавляем Sheets writeback на время bulk-обработки ──────────
        # Без этого 264 сообщения × 3 batch-writeback'а = 800+ API calls →
        # Google quota 300/min → 429 storm. В конце один раз вызываем resync*
        # команды которые ОДНИМ проходом записывают финальное состояние всех
        # затронутых HAWB / Cargo. Если dry-run — никаких записей не нужно.
        from cargo.services.sheets.writeback import (
            begin_batch_writeback, end_batch_writeback,
        )
        if not opts['dry_run']:
            begin_batch_writeback()

        changed = 0
        gtd_changed = 0
        errors = 0
        for i, pk in enumerate(pks, 1):
            for attempt in range(3):
                try:
                    m = AltaInboxMessage.objects.get(pk=pk)
                    new_meta_full = parse_raw_xml(m.raw_xml or '')
                    old = m.parsed_meta or {}
                    # Сохраняем те поля что добавлял dispatch (apply_error и пр.)
                    preserved = {k: v for k, v in old.items()
                                 if k in ('apply_error', 'source_file')}
                    new_meta = {**new_meta_full, **preserved}

                    old_gtd = (old.get('gtd_number') or '').strip()
                    new_gtd = (new_meta.get('gtd_number') or '').strip()
                    diff = new_meta != old

                    if not diff and not opts['force_dispatch']:
                        break

                    if old_gtd != new_gtd:
                        gtd_changed += 1
                        self.stdout.write(
                            f'  #{pk}  gtd_number: {old_gtd!r} → {new_gtd!r}  '
                            f'({m.msg_type})')

                    if opts['dry_run']:
                        break

                    if diff:
                        m.parsed_meta = new_meta
                        m.save(update_fields=['parsed_meta'])
                        changed += 1
                    # Передиспатчить чтобы classify/match/recompute применили.
                    # При --force-dispatch вызываем даже если parsed_meta не менялся —
                    # нужно при обновлении логики apply_status/match (multi-waybill,
                    # TZ-fix и пр.).
                    dispatch(m)
                    break
                except OperationalError as e:
                    if 'locked' in str(e).lower() and attempt < 2:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    errors += 1
                    self.stdout.write(f'  ERR #{pk}: {e}')
                    break
                except Exception as e:
                    errors += 1
                    if errors < 10:
                        self.stdout.write(f'  ERR #{pk}: {e}')
                    break
            if i % 200 == 0:
                self.stdout.write(f'  progress: {i}/{len(pks)} changed={changed} '
                                  f'gtd_changed={gtd_changed} errors={errors}')

        # Снимаем подавление, делаем resync Sheets для всех затронутых HAWB/Cargo.
        if not opts['dry_run']:
            end_batch_writeback()

            self.stdout.write('')
            self.stdout.write(self.style.NOTICE('Sheets resync (после bulk-reparse)...'))
            try:
                from cargo.models import HouseWaybill, Cargo
                from cargo.services.sheets.writeback import (
                    batch_write_declarations_for_hawbs,
                    batch_write_filed_dates_for_hawbs,
                    batch_write_release_dates_for_hawbs,
                    batch_write_svh_for_cargos,
                )
                # ДТ-номера
                hawbs_decl = list(HouseWaybill.objects.exclude(customs_declaration_number=''))
                if hawbs_decl:
                    n = batch_write_declarations_for_hawbs(hawbs_decl)
                    self.stdout.write(f'  decl: {n} cells ({len(hawbs_decl)} HAWB)')
                # filed_date / release_date: включаем ВСЕ HAWB у которых
                # есть привязанное CMN.11350 (или filed/release_date уже
                # стоят) — нужно чтобы при per-Consignment пересчёте
                # ОЧИЩАЛИСЬ ячейки HAWB которые ранее были ошибочно
                # помечены released, а на самом деле получили отказ/запрос
                # документов (release_date теперь None → пустая ячейка).
                from django.db.models import Q
                hawbs_touched = list(HouseWaybill.objects.filter(
                    Q(filed_date__isnull=False)
                    | Q(release_date__isnull=False)
                    | Q(inbox_messages__msg_type='CMN.11350')
                ).distinct())
                if hawbs_touched:
                    n = batch_write_filed_dates_for_hawbs(hawbs_touched)
                    self.stdout.write(f'  filed_date: {n} cells ({len(hawbs_touched)} HAWB)')
                    n = batch_write_release_dates_for_hawbs(hawbs_touched)
                    self.stdout.write(f'  release_date: {n} cells ({len(hawbs_touched)} HAWB)')
                # SVH (cargo-level: license/scan_into_bond/svh_do1_reg_number)
                cargos_svh = list(Cargo.objects.filter(scan_into_bond__isnull=False)
                                  .exclude(svh_do1_reg_number=''))
                if cargos_svh:
                    n = batch_write_svh_for_cargos(cargos_svh)
                    self.stdout.write(f'  svh: {n} cells ({len(cargos_svh)} cargos)')
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Sheets resync failed: {e}'))

        self.stdout.write(self.style.SUCCESS(
            f'Done. processed={len(pks)} parsed_meta_changed={changed} '
            f'gtd_number_changed={gtd_changed} errors={errors}'
        ))
