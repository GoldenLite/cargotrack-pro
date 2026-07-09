"""Аудит HAWB: ищем накладные у которых в inbox есть release-сообщения
(CMN.11350, CMN.11010, CMN.11309), но в БД статус НЕ RELEASED. С флагом
--apply сам прогоняет re-dispatch + writeback в Sheets. Под cron: фикс
race/silent-fail при первичном dispatch (status_applied=False без
apply_error) без необходимости разбираться где именно теряется.
"""
import logging
from collections import defaultdict

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, HouseWaybill


logger = logging.getLogger('cargo.audit_releases')


# Типы сообщений-выпусков. CMN.11350 (per-HAWB через consignments),
# CMN.11010 (ED_Container с одной HAWB), CMN.11309 (ExpressNotification).
RELEASE_MSG_TYPES = ('CMN.11350', 'CMN.11010', 'CMN.11309')


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Реально дернуть re-dispatch + writeback')
        parser.add_argument('--quiet', action='store_true',
                            help='Логи только если есть что чинить')

    def handle(self, *args, **opts):
        quiet = opts['quiet']

        msgs = AltaInboxMessage.objects.filter(
            msg_type__in=RELEASE_MSG_TYPES,
            msg_kind='released',
        ).order_by('prepared_at')
        if not quiet:
            self.stdout.write(f'Total release msgs in DB: {msgs.count()}')

        waybill_to_releases = defaultdict(list)
        for m in msgs.iterator(chunk_size=500):
            meta = m.parsed_meta or {}
            # CMN.11350: per-HAWB consignments c decision_code=10
            cons = meta.get('consignments') or []
            if cons:
                for c in cons:
                    if (c.get('decision_code') or '').strip() != '10':
                        continue
                    for w in c.get('waybills') or []:
                        waybill_to_releases[w].append(m)
                continue
            # CMN.11010 / CMN.11309: HAWB в hawb FK (привязан в dispatch).
            if m.hawb_id:
                hawb_num = HouseWaybill.objects.filter(
                    pk=m.hawb_id
                ).values_list('hawb_number', flat=True).first()
                if hawb_num:
                    waybill_to_releases[hawb_num].append(m)
            # Также waybill_number из parsed_meta
            wn = (meta.get('waybill_number') or '').strip()
            if wn:
                waybill_to_releases[wn].append(m)

        hawbs = {h.hawb_number: h for h in HouseWaybill.objects
                 .filter(hawb_number__in=list(waybill_to_releases.keys()))
                 .select_related('mawb')}

        problems = []
        for waybill, releases in waybill_to_releases.items():
            h = hawbs.get(waybill)
            if not h:
                continue  # NO_HAWB_IN_DB — чужая компания, не наша
            if h.customs_status == 'RELEASED':
                continue
            problems.append((waybill, h, releases))

        if quiet and not problems:
            return

        self.stdout.write(f'Problems: {len(problems)}')

        if not problems:
            return

        for waybill, h, releases in problems:
            self.stdout.write(
                f'  {waybill}: status={h.customs_status or "(пусто)"!r} '
                f'decl={h.customs_declaration_number!r} '
                f'type={h.shipment_type}')

        if not opts['apply']:
            self.stdout.write('\n--apply not given — отчёт без починки')
            return

        self.stdout.write('\n=== Applying fix ===')
        from django.utils.dateparse import parse_datetime
        from cargo.services.alta.inbox import _retry_on_locked
        fixed: list[HouseWaybill] = []

        # Индекс waybill → релевантные сообщения строим ОДНИМ проходом по
        # inbox (раньше был вложенный скан всей inbox НА КАЖДУЮ проблему —
        # O(проблемы × сообщения), из-за чего команда зависала под cron'ом,
        # 09.07.2026). Теперь O(сообщения): один проход, фильтр по set
        # проблемных waybills + map pk→waybill для hawb_id-совпадений.
        problem_waybills = {waybill for waybill, _, _ in problems}
        pk_to_waybill = {h.pk: waybill for waybill, h, _ in problems}
        msgs_by_waybill: dict[str, list] = defaultdict(list)
        idx_qs = AltaInboxMessage.objects.filter(
            msg_type__in=('CMN.11350', 'CMN.11337', 'CMN.11001',
                          'CMN.11309', 'CMN.11010')
        ).order_by('prepared_at')
        for m in idx_qs.iterator(chunk_size=500):
            meta = m.parsed_meta or {}
            hit: set[str] = set()
            for c in meta.get('consignments') or []:
                for w in c.get('waybills') or []:
                    if w in problem_waybills:
                        hit.add(w)
            wn = (meta.get('waybill_number') or '').strip()
            if wn in problem_waybills:
                hit.add(wn)
            if m.hawb_id in pk_to_waybill:
                hit.add(pk_to_waybill[m.hawb_id])
            for w in hit:
                msgs_by_waybill[w].append(m)

        # ПРИМЕНЕНИЕ через bulk_update, НЕ dispatch. Урок сессии
        # (redispatch_stuck_finals --lean): dispatch на каждую HAWB под
        # cron-конкуренцией = ~3 мин/HAWB (лок + инлайн-writeback) →
        # 114 backlog = часы, reaper убивал прогон. Lean = одна транзакция:
        #   1) auto-match unmatched (привязать cargo/hawb по MAWB),
        #   2) гард newer_final (не перебивать более свежий отказ/отзыв),
        #   3) bulk_update customs_status=RELEASED + release_date.
        # Листы догоняет AuditFixExport (--kind export --fix, 20 мин).
        to_match: list[AltaInboxMessage] = []
        to_release: dict[int, HouseWaybill] = {}
        skipped_newer = 0
        for waybill, h, releases in problems:
            # 1) auto-match unmatched сообщения этого waybill
            for m in msgs_by_waybill.get(waybill, []):
                if m.cargo_id is None and m.hawb_id is None and h.mawb:
                    m.cargo = h.mawb
                    m.hawb = h
                    to_match.append(m)

            if h.customs_status == 'RELEASED':
                continue

            # release_prepared = самое свежее release-сообщение по waybill
            rel_prepared = max((m.prepared_at for m in releases
                                if m.prepared_at), default=None)
            # 2) гард: есть ли ПОЗЖЕ отказ/отзыв по этой HAWB → не выпускать
            if rel_prepared and h.pk:
                newer = (AltaInboxMessage.objects
                         .filter(hawb_id=h.pk, prepared_at__gt=rel_prepared,
                                 msg_kind__in=('rejected', 'withdrawn'))
                         .exists())
                if newer:
                    skipped_newer += 1
                    continue

            # release_date: decision_date per-waybill из consignment, иначе
            # prepared_at самого свежего release.
            rel_dt = None
            for m in sorted(releases, key=lambda x: x.prepared_at or rel_prepared,
                            reverse=True):
                meta = m.parsed_meta or {}
                for c in meta.get('consignments') or []:
                    if waybill in (c.get('waybills') or []):
                        rel_dt = parse_datetime(c.get('decision_date') or '') \
                            or None
                        break
                if rel_dt:
                    break
            h.customs_status = 'RELEASED'
            h.release_date = rel_dt or rel_prepared
            to_release[h.pk] = h
            fixed.append(h)

        # Оба bulk_update в ОДНОЙ короткой atomic-транзакции: под
        # cron-конкуренцией серия отдельных UPDATE ловит 'database is
        # locked' между собой; atomic делает всё разом и быстро отпускает
        # write-lock. attempts=12 (backoff до 8с ≈ 80с суммарно) на случай
        # долгого чужого writer'а.
        from django.db import transaction

        def _apply_bulk():
            with transaction.atomic():
                if to_match:
                    AltaInboxMessage.objects.bulk_update(
                        to_match, ['cargo', 'hawb'], batch_size=200)
                if to_release:
                    HouseWaybill.objects.bulk_update(
                        list(to_release.values()),
                        ['customs_status', 'release_date'], batch_size=100)

        if to_match or to_release:
            _retry_on_locked(_apply_bulk, attempts=12)
        self.stdout.write(self.style.SUCCESS(
            f'выпущено (bulk): {len(to_release)}, привязано сообщений: '
            f'{len(to_match)}, пропущено (свежий отказ): {skipped_newer}'))

        # Writeback в Sheets для всех починенных.
        if fixed:
            from cargo.services.sheets.writeback import (
                batch_write_declarations_for_hawbs,
                batch_write_release_dates_for_hawbs,
                batch_write_filed_dates_for_hawbs,
                batch_write_ed_status_for_hawbs,
            )
            batch_write_declarations_for_hawbs(fixed)
            batch_write_release_dates_for_hawbs(fixed)
            batch_write_filed_dates_for_hawbs(fixed)
            batch_write_ed_status_for_hawbs(fixed)
            self.stdout.write(f'Writeback done for {len(fixed)} HAWB')
