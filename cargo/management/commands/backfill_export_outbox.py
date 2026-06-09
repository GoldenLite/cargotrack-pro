"""Backfill для export outbox-наблюдений.

Проблема: _apply_export_outbox был развёрнут позже чем приходили сообщения
(или какие-то obs обрабатывались до подключения парсера). У 70+ HAWB
declaration_form, declarant_name, transport_doc остались пустыми, хотя
в raw_xml эти данные были.

Эта команда пробегает все AltaOutboxObservation типов CMN.11335/11349/11024
за окно и вызывает _apply_export_outbox(obs) повторно. Operations идемпотентны
(HouseWaybill.objects.filter(...).update() с проверкой текущего значения).

В конце — один общий writeback для всех уникальных affected HAWBs, чтобы не
дёргать Sheets API на каждое obs.
"""
from __future__ import annotations

import datetime
import logging

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from cargo.models import AltaOutboxObservation, HouseWaybill
from cargo.services.alta.outbox import (
    _parse_export_obs, _ensure_export_cargo, _ensure_export_hawb,
    _DECL_FORM_BY_MSG_TYPE, _filed_date_should_replace,
    _writeback_export_hawbs,
)


logger = logging.getLogger('cargo.backfill.export')


class Command(BaseCommand):
    help = 'Backfill _apply_export_outbox для existing AltaOutboxObservation'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30,
                            help='Окно (дни) для отбора obs (default 30)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Не писать, только показать какие HAWB бы обновились')
        parser.add_argument('--skip-writeback', action='store_true',
                            help='Не вызывать Sheets writeback (только БД)')
        parser.add_argument('--limit', type=int, default=0,
                            help='Только первые N obs (0 = все)')

    def handle(self, *args, **opts):
        days = opts['days']
        cutoff = timezone.now() - datetime.timedelta(days=days)
        qs = (AltaOutboxObservation.objects
              .filter(msg_type__in=['CMN.11335','CMN.11349','CMN.11024'])
              .filter(prepared_at__gte=cutoff)
              .order_by('-prepared_at'))
        total = qs.count()
        if opts['limit']:
            qs = qs[:opts['limit']]
        self.stdout.write(f'Период: с {cutoff:%d.%m.%Y} ({days}d), '
                          f'найдено obs: {total}, обработаем: '
                          f'{opts["limit"] or total}')

        affected_hawb_ids: set[int] = set()
        n_obs_processed = n_obs_skipped_not_export = n_obs_no_parsed = 0
        n_fields_updated = 0
        n_hawbs_per_msg_type: dict[str, int] = {}

        for obs in qs.iterator(chunk_size=100):
            parsed = _parse_export_obs(obs)
            if not parsed:
                n_obs_no_parsed += 1
                continue
            if not parsed['is_export']:
                n_obs_skipped_not_export += 1
                continue
            n_obs_processed += 1
            decl_form = _DECL_FORM_BY_MSG_TYPE.get(obs.msg_type, '')
            signatory = (parsed.get('signatory') or '').strip()

            for hawb_num in parsed['hawbs']:
                transport_doc = parsed['transport_per_hawb'].get(hawb_num, '')
                cargo = _ensure_export_cargo(transport_doc) if transport_doc and not opts['dry_run'] else None
                if opts['dry_run']:
                    h = HouseWaybill.objects.filter(hawb_number__iexact=hawb_num).first()
                    if not h:
                        continue
                else:
                    h = _ensure_export_hawb(hawb_num, cargo)
                    if not h:
                        continue

                update_fields: dict = {}
                if decl_form and h.declaration_form != decl_form:
                    update_fields['declaration_form'] = decl_form
                if signatory and h.declarant_name != signatory:
                    update_fields['declarant_name'] = signatory
                if obs.prepared_at and _filed_date_should_replace(
                        h.filed_date, obs.prepared_at):
                    update_fields['filed_date'] = obs.prepared_at
                per_hawb_count = (parsed['goods_count_per_hawb'].get(hawb_num)
                                  or parsed['goods_count'])
                if per_hawb_count and h.goods_count != per_hawb_count:
                    update_fields['goods_count'] = per_hawb_count

                if update_fields and not opts['dry_run']:
                    HouseWaybill.objects.filter(pk=h.pk).update(**update_fields)
                    affected_hawb_ids.add(h.pk)
                    n_fields_updated += len(update_fields)
                    n_hawbs_per_msg_type[obs.msg_type] = (
                        n_hawbs_per_msg_type.get(obs.msg_type, 0) + 1)
                elif update_fields:
                    affected_hawb_ids.add(h.pk)
                    n_fields_updated += len(update_fields)
                    self.stdout.write(
                        f'  DRY: {hawb_num} obs={obs.id} ({obs.msg_type}): '
                        f'{", ".join(update_fields.keys())}')

            if n_obs_processed % 50 == 0:
                self.stdout.write(
                    f'  ... {n_obs_processed} obs обработано, '
                    f'{len(affected_hawb_ids)} HAWB затронуто')

        self.stdout.write(
            f'\nИтого: obs_processed={n_obs_processed} '
            f'no_parsed={n_obs_no_parsed} not_export={n_obs_skipped_not_export} '
            f'fields_updated={n_fields_updated} hawbs_affected={len(affected_hawb_ids)}')
        for mt, n in n_hawbs_per_msg_type.items():
            self.stdout.write(f'  {mt}: {n} HAWB-обновлений')

        if opts['dry_run']:
            self.stdout.write('DRY RUN — БД не изменена.')
            return
        if opts['skip_writeback']:
            self.stdout.write('skip_writeback — Sheets не трогали.')
            return
        if not affected_hawb_ids:
            self.stdout.write('Нет затронутых HAWB — Sheets writeback пропускаем.')
            return

        self.stdout.write(f'Запускаю Sheets writeback для {len(affected_hawb_ids)} HAWB...')
        hawbs = list(HouseWaybill.objects.filter(pk__in=affected_hawb_ids))
        _writeback_export_hawbs(hawbs)
        self.stdout.write(self.style.SUCCESS('Backfill done.'))
