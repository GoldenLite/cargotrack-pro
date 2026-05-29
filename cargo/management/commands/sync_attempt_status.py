"""Синхронизировать HawbDeclarationAttempt.status с HouseWaybill.customs_status.

Сценарий: HAWB.customs_status='RELEASED' (apply_status сработал),
но attempt для current_decl застрял в FILED — bug: apply не вызывал
_register_attempt с обновлённым status. После исправления apply новые
HAWB корректны, эта команда чинит старые.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import HawbDeclarationAttempt, HouseWaybill


class Command(BaseCommand):
    help = 'Sync HawbDeclarationAttempt.status с HouseWaybill.customs_status.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        # HAWB с RELEASED где attempt для current decl ещё FILED.
        qs = HouseWaybill.objects.filter(customs_status='RELEASED').exclude(
            customs_declaration_number='')
        to_fix = []
        for h in qs:
            decl = h.customs_declaration_number
            att = HawbDeclarationAttempt.objects.filter(
                hawb=h, declaration_number=decl).first()
            if att and att.status != 'RELEASED':
                to_fix.append((h, att))
        self.stdout.write(f'Found RELEASED HAWBs with stale FILED attempt: '
                          f'{len(to_fix)}')
        for h, att in to_fix[:20]:
            self.stdout.write(
                f'  {h.hawb_number}  decl={att.declaration_number}  '
                f'att.status={att.status}  hawb.release_date={h.release_date}')
        if opts['dry_run']:
            return
        for h, att in to_fix:
            att.status = 'RELEASED'
            if not att.release_date and h.release_date:
                att.release_date = h.release_date
            att.save(update_fields=['status', 'release_date'])
        self.stdout.write(self.style.SUCCESS(f'Updated: {len(to_fix)}'))

        # Аналог для REJECTED
        qs2 = HouseWaybill.objects.filter(customs_status='REJECTED').exclude(
            customs_declaration_number='')
        to_fix2 = []
        for h in qs2:
            decl = h.customs_declaration_number
            att = HawbDeclarationAttempt.objects.filter(
                hawb=h, declaration_number=decl).first()
            if att and att.status != 'REJECTED':
                to_fix2.append((h, att))
        self.stdout.write(f'REJECTED HAWBs with stale attempt: {len(to_fix2)}')
        for h, att in to_fix2:
            att.status = 'REJECTED'
            att.save(update_fields=['status'])
        self.stdout.write(self.style.SUCCESS(f'REJECTED updated: {len(to_fix2)}'))
