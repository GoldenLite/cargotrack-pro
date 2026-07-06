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
        from cargo.services.alta.inbox import dispatch
        fixed: list[HouseWaybill] = []
        for waybill, h, releases in problems:
            # Auto-match unmatched через MAWB.
            all_msgs = []
            qs = AltaInboxMessage.objects.filter(
                msg_type__in=('CMN.11350', 'CMN.11337', 'CMN.11001',
                              'CMN.11309', 'CMN.11010')
            ).order_by('prepared_at')
            for m in qs.iterator(chunk_size=500):
                meta = m.parsed_meta or {}
                hit = False
                for c in meta.get('consignments') or []:
                    if waybill in (c.get('waybills') or []):
                        hit = True
                        break
                if not hit and meta.get('waybill_number') == waybill:
                    hit = True
                if not hit and m.hawb_id == h.pk:
                    hit = True
                if hit:
                    all_msgs.append(m)

            for m in all_msgs:
                if m.cargo_id is None and m.hawb_id is None and h.mawb:
                    m.cargo = h.mawb
                    m.hawb = h
                    m.save(update_fields=['cargo', 'hawb'])

            for m in all_msgs:
                try:
                    dispatch(m)
                except Exception:
                    logger.exception(
                        'audit_export_releases: dispatch failed env=%s',
                        m.envelope_id)

            h.refresh_from_db()
            if h.customs_status == 'RELEASED':
                fixed.append(h)
                self.stdout.write(
                    f'  ✓ {waybill}: → RELEASED decl={h.customs_declaration_number}')
            else:
                self.stdout.write(self.style.WARNING(
                    f'  ✗ {waybill}: still {h.customs_status}'))

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
            self.stdout.write(f'\nWriteback done for {len(fixed)} HAWB')
