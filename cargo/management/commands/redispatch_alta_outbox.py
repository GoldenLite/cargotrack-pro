"""Передиспатчить все AltaOutboxObservation — backfill после изменений в outbox.dispatch.

Аналог reparse_alta_inbox, но для исходящих наблюдений (538134^*.gz).
Используется после миграций, которые меняют какие поля заполняет dispatch
(например, 0055: svh_do1_sent_at Cargo→HouseWaybill — миграция не переносит
данные, нужен передиспатч для восстановления).

Перед dispatch re-парсит raw_xml (если есть в parsed_meta) — освежает
hawbs/goods/certificate_number/mawb. Старые agent'ы могли не присылать
полный hawbs список, но потом отправили raw_xml — теперь подхватим.

Запуск:
    uv run python manage.py redispatch_alta_outbox                  # все
    uv run python manage.py redispatch_alta_outbox --type ED.DO1    # только ED.DO1
    uv run python manage.py redispatch_alta_outbox --diag           # только статистика
    uv run python manage.py redispatch_alta_outbox --dry-run
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db import connection, OperationalError

from cargo.models import AltaOutboxObservation
from cargo.services.alta.outbox import dispatch


class Command(BaseCommand):
    help = 'Передиспатчить AltaOutboxObservation (backfill после миграций outbox.dispatch)'

    def add_arguments(self, parser):
        parser.add_argument('--type', dest='msg_type', default='',
                            help='Только указанный msg_type (например ED.DO1)')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--diag', action='store_true',
                            help='Только статистика по полям parsed_meta')

    def handle(self, *args, **opts):
        if connection.vendor == 'sqlite':
            with connection.cursor() as c:
                c.execute('PRAGMA busy_timeout=120000;')

        qs = AltaOutboxObservation.objects.all().order_by('prepared_at')
        if opts['msg_type']:
            qs = qs.filter(msg_type=opts['msg_type'])
        if opts['limit']:
            qs = qs[:opts['limit']]

        pks = list(qs.values_list('pk', flat=True))
        self.stdout.write(f'Redispatch: {len(pks)} observations, '
                          f'dry_run={opts["dry_run"]} diag={opts["diag"]}')

        # ── Диагностика ──
        # Считаем сколько observations имеют какие поля. Помогает понять
        # почему apply_*  не отрабатывает (если 0 имеют prepared_at — ясно).
        if pks:
            stats = {
                'total': len(pks),
                'with_prepared_at': 0,
                'with_raw_xml': 0,
                'with_hawbs_meta': 0,
                'with_goods_meta': 0,
                'with_cargo_linked': 0,
                'hawbs_total': 0,
                'hawbs_max': 0,
            }
            for obs in qs.iterator():
                pm = obs.parsed_meta or {}
                if obs.prepared_at:
                    stats['with_prepared_at'] += 1
                if pm.get('raw_xml'):
                    stats['with_raw_xml'] += 1
                hawbs = pm.get('hawbs') or []
                if hawbs:
                    stats['with_hawbs_meta'] += 1
                    stats['hawbs_total'] += len(hawbs)
                    stats['hawbs_max'] = max(stats['hawbs_max'], len(hawbs))
                if pm.get('goods'):
                    stats['with_goods_meta'] += 1
                if obs.cargo_id:
                    stats['with_cargo_linked'] += 1
            self.stdout.write('')
            self.stdout.write(self.style.NOTICE('Diagnostics:'))
            for k, v in stats.items():
                self.stdout.write(f'  {k}: {v}')
            self.stdout.write('')

        if opts['diag']:
            return

        # Подавляем per-вызов sheets writeback — в конце один resync.
        from cargo.services.sheets.writeback import (
            begin_batch_writeback, end_batch_writeback,
        )
        if not opts['dry_run']:
            begin_batch_writeback()

        # Re-parse функция: если raw_xml есть, прогоняем через parse_do1_report
        # и обогащаем parsed_meta (хапбсы, гудс, серт, мавб). Аналог логики
        # view api_alta_outbox_post.
        from cargo.services.alta.xml_extract import parse_do1_report

        def _refresh_parsed_meta(obs) -> bool:
            """Возвращает True если parsed_meta обновлён."""
            if obs.msg_type != 'ED.DO1':
                return False
            pm = obs.parsed_meta or {}
            raw = pm.get('raw_xml')
            if not raw:
                return False
            try:
                parsed = parse_do1_report(raw)
            except Exception:
                return False
            changed = False
            for key in ('hawbs', 'goods', 'report_number', 'certificate_number'):
                val = parsed.get(key)
                if val and pm.get(key) != val:
                    pm[key] = val
                    changed = True
            mawb = parsed.get('mawb')
            if mawb and obs.common_waybill_number != mawb[:64]:
                obs.common_waybill_number = mawb[:64]
                changed = True
            if changed:
                obs.parsed_meta = pm
            return changed

        errors = 0
        refreshed = 0
        BACKOFF = [1, 2, 4, 8, 16, 32]
        for i, pk in enumerate(pks, 1):
            obs = None
            for attempt in range(len(BACKOFF) + 1):
                try:
                    obs = AltaOutboxObservation.objects.get(pk=pk)
                    if not opts['dry_run']:
                        if _refresh_parsed_meta(obs):
                            obs.save(update_fields=['parsed_meta',
                                                    'common_waybill_number'])
                            refreshed += 1
                        dispatch(obs)
                    break
                except OperationalError as e:
                    if 'locked' in str(e).lower() and attempt < len(BACKOFF):
                        time.sleep(BACKOFF[attempt])
                        continue
                    errors += 1
                    if errors < 10:
                        self.stdout.write(f'  ERR #{pk}: {e}')
                    break
                except Exception as e:
                    errors += 1
                    if errors < 10:
                        import traceback
                        tp = obs.msg_type if obs else "?"
                        self.stdout.write(f'  ERR #{pk} ({tp}): {e}')
                        self.stdout.write(traceback.format_exc())
                    break
            if i % 200 == 0:
                self.stdout.write(f'  progress: {i}/{len(pks)} '
                                  f'refreshed={refreshed} errors={errors}')

        if not opts['dry_run']:
            end_batch_writeback()

            # Sheets resync — пишем только per-HAWB svh_do1_sent_at,
            # weight/places + cargo-level svh.
            self.stdout.write('')
            self.stdout.write(self.style.NOTICE('Sheets resync...'))
            try:
                from cargo.models import HouseWaybill, Cargo, ImportedSheetRow
                from cargo.services.sheets.writeback import (
                    batch_write_svh_for_cargos,
                    batch_write_svh_do1_sent_for_hawbs,
                    batch_write_svh_do1_weight_for_hawbs,
                    batch_write_svh_do1_places_for_hawbs,
                )
                hawb_nums_in_sheets = list(
                    ImportedSheetRow.objects.filter(source__kind='general')
                    .values_list('hawb_number_norm', flat=True).distinct()
                )
                hawbs_for_do1 = list(HouseWaybill.objects.filter(
                    hawb_number__in=hawb_nums_in_sheets
                ).distinct())
                if hawbs_for_do1:
                    n = batch_write_svh_do1_sent_for_hawbs(hawbs_for_do1)
                    self.stdout.write(f'  svh_do1_sent: {n} cells ({len(hawbs_for_do1)} HAWB)')
                    n = batch_write_svh_do1_weight_for_hawbs(hawbs_for_do1)
                    self.stdout.write(f'  svh_do1_weight: {n} cells')
                    n = batch_write_svh_do1_places_for_hawbs(hawbs_for_do1)
                    self.stdout.write(f'  svh_do1_places: {n} cells')
                cargos_svh = list(Cargo.objects.filter(hawbs__isnull=False).distinct())
                if cargos_svh:
                    n = batch_write_svh_for_cargos(cargos_svh)
                    self.stdout.write(f'  svh: {n} cells ({len(cargos_svh)} cargos)')
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Sheets resync failed: {e}'))

        self.stdout.write(self.style.SUCCESS(
            f'Done. processed={len(pks)} refreshed={refreshed} errors={errors}'))
