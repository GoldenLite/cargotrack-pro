"""Аудит HAWB где customs_declaration_number разошёлся с реальной
released-подачей (баг _sync_decl_via_outbox: кросс-пропагация decl
через общий HAWB между двумя подачами одной партии)."""
from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill, HawbDeclarationAttempt


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Восстановить decl + filed_date из RELEASED-attempt')

    def handle(self, *args, **opts):
        problems = []
        for h in HouseWaybill.objects.filter(
                customs_status='RELEASED',
                release_date__isnull=False
        ).exclude(customs_declaration_number=''):
            # Найти RELEASED-attempt с release_date == h.release_date
            ra = h.declaration_attempts.filter(
                status='RELEASED', release_date=h.release_date
            ).order_by('-release_date').first()
            if not ra:
                continue
            if ra.declaration_number != h.customs_declaration_number:
                problems.append({
                    'hawb': h.hawb_number,
                    'current_decl': h.customs_declaration_number,
                    'released_decl': ra.declaration_number,
                    'released_at': h.release_date,
                    'filed_date': h.filed_date,
                    'release_attempt_filed': getattr(ra, 'filed_date', None),
                })

        self.stdout.write(f'Found {len(problems)} HAWB с несовпадением decl')
        for p in problems[:30]:
            self.stdout.write(
                f"  {p['hawb']}: current={p['current_decl']} "
                f"!= released={p['released_decl']} (released at {p['released_at']})")
        if len(problems) > 30:
            self.stdout.write(f'  ... ещё {len(problems) - 30}')

        if not opts['apply'] or not problems:
            return

        self.stdout.write('\n=== Applying restore ===')
        from cargo.services.sheets.writeback import (
            batch_write_declarations_for_hawbs,
            batch_write_filed_dates_for_hawbs,
        )
        fixed = []
        for p in problems:
            h = HouseWaybill.objects.filter(hawb_number=p['hawb']).first()
            if not h:
                continue
            updates = {'customs_declaration_number': p['released_decl']}
            # filed_date: если у released-attempt есть filed_date — берём его
            if p['release_attempt_filed']:
                updates['filed_date'] = p['release_attempt_filed']
            HouseWaybill.objects.filter(pk=h.pk).update(**updates)
            h.refresh_from_db()
            fixed.append(h)
            self.stdout.write(
                f'  ✓ {h.hawb_number}: decl→{p["released_decl"]} '
                f'(was {p["current_decl"]})')

        # Writeback в Sheets
        if fixed:
            batch_write_declarations_for_hawbs(fixed)
            batch_write_filed_dates_for_hawbs(fixed)
            self.stdout.write(f'\nWriteback {len(fixed)} HAWB')
