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
                # 120с busy_timeout — reparse идёт параллельно с агентом
                # (входящие CMN от него летят через HTTP в waitress) и
                # фоновыми потоками Sheets writeback. Без длинного timeout
                # 12k сообщений уверенно бьются о SQLite-локи.
                c.execute('PRAGMA busy_timeout=120000;')

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

        # Одноразовый ресет stale-полей перед --force-dispatch.
        # match_svh_do2 (commit 30c4c36) больше НЕ матчит ДО2 по
        # customs_declaration_number — только по прямому hawb_number в
        # TransportDoc. Но apply_svh_do2 только записывает в matched HAWB,
        # не очищает в не-matched. Старые stale-значения (записанные раньше
        # через ДТ-matching) висят в БД. Зануляем — dispatch заново
        # проставит правильно только тем HAWB кто реально в TransportDoc.
        if not opts['dry_run'] and opts['force_dispatch']:
            from cargo.models import HouseWaybill
            n_do2 = HouseWaybill.objects.filter(
                svh_do2_send_at__isnull=False).update(svh_do2_send_at=None)
            self.stdout.write(
                f'Reset before reparse: svh_do2_send_at on {n_do2} HAWBs')

        changed = 0
        gtd_changed = 0
        errors = 0
        # SQLite-локи на параллельной нагрузке (агент + waitress + writeback)
        # бьют по reparse'у. Экспоненциальный backoff с 6 попытками
        # (~1+2+4+8+16+32 = 63 сек суммарной паузы на одно сообщение в
        # худшем случае) — достаточно чтобы пережить даже самые жёсткие
        # пики записи.
        BACKOFF = [1, 2, 4, 8, 16, 32]
        for i, pk in enumerate(pks, 1):
            for attempt in range(len(BACKOFF) + 1):
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
                    if 'locked' in str(e).lower() and attempt < len(BACKOFF):
                        time.sleep(BACKOFF[attempt])
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
                    batch_write_svh_do2_dates_for_hawbs,
                    batch_write_svh_for_cargos,
                    drop_deprecated_columns,
                )
                # Одноразовая очистка: убрать колонки которые мы больше не
                # пишем (юзер их не использует).
                n_dropped = drop_deprecated_columns()
                if n_dropped:
                    self.stdout.write(f'  dropped {n_dropped} deprecated column(s)')
                # decl/filed_date/release_date: включаем ВСЕ HAWB у которых
                # есть привязанное CMN.11350 (или эти поля уже стоят) — нужно
                # чтобы при per-Consignment пересчёте ОЧИЩАЛИСЬ ячейки HAWB
                # которые ранее были ошибочно помечены released, а на самом
                # деле получили отказ/запрос документов (поля теперь пустые
                # → ячейки тоже должны стать пустыми).
                from django.db.models import Q
                # Включаем И HAWB у которых был CMN.13014 (ДО2) — нужно
                # пройтись и переписать «дата ДО2», для не-затронутых
                # очистить стейл (после переноса поля Cargo→HouseWaybill
                # старые ячейки растянулись на всю партию).
                hawbs_touched = list(HouseWaybill.objects.filter(
                    Q(filed_date__isnull=False)
                    | Q(release_date__isnull=False)
                    | Q(svh_do2_send_at__isnull=False)
                    | Q(customs_declaration_number__gt='')
                    | Q(inbox_messages__msg_type__in=('CMN.11350', 'CMN.13014'))
                    | Q(mawb__hawbs__inbox_messages__msg_type='CMN.13014')
                ).distinct())
                if hawbs_touched:
                    n = batch_write_declarations_for_hawbs(hawbs_touched)
                    self.stdout.write(f'  decl: {n} cells ({len(hawbs_touched)} HAWB)')
                    n = batch_write_filed_dates_for_hawbs(hawbs_touched)
                    self.stdout.write(f'  filed_date: {n} cells ({len(hawbs_touched)} HAWB)')
                    n = batch_write_release_dates_for_hawbs(hawbs_touched)
                    self.stdout.write(f'  release_date: {n} cells ({len(hawbs_touched)} HAWB)')
                    n = batch_write_svh_do2_dates_for_hawbs(hawbs_touched)
                    self.stdout.write(f'  svh_do2: {n} cells ({len(hawbs_touched)} HAWB)')
                # SVH (cargo-level: license/scan_into_bond/svh_do1_reg_number).
                # Берём ВСЕ Cargos с HAWB в Sheets «Общее». batch_write_svh_for_cargos
                # сравнивает значение с тем что в ячейке и пишет только при
                # различии — для партий без svh-данных, у которых Sheets-ячейка
                # пустая, будет no-op. Для откачённых (data=пусто, Sheets=стейл)
                # будет очистка. Это надёжнее чем пытаться через FK ловить
                # партии с inbox-привязкой — при откате msg.cargo тоже мог
                # обнулиться (как у Cargo 141-70338343).
                cargos_svh = list(Cargo.objects.filter(hawbs__isnull=False).distinct())
                if cargos_svh:
                    n = batch_write_svh_for_cargos(cargos_svh)
                    self.stdout.write(f'  svh: {n} cells ({len(cargos_svh)} cargos)')
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Sheets resync failed: {e}'))

        self.stdout.write(self.style.SUCCESS(
            f'Done. processed={len(pks)} parsed_meta_changed={changed} '
            f'gtd_number_changed={gtd_changed} errors={errors}'
        ))
