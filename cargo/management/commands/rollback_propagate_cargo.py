"""Откатить propagate_cargo_release: найти HAWB которые получили
RELEASED+decl ТОЛЬКО через cargo-level пропагацию (без inbox-факта)
и очистить их.

Признак propagation: customs_status='RELEASED' + НИ ОДНОГО inbox-msg
не упоминает эту HAWB ни по FK ни через raw_xml.icontains.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from cargo.models import AltaInboxMessage, HawbDeclarationAttempt, HouseWaybill


class Command(BaseCommand):
    help = 'Rollback propagate_cargo_release.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        # HAWB с RELEASED статусом
        qs = HouseWaybill.objects.filter(customs_status='RELEASED')
        rolled = 0
        for h in qs:
            if not h.hawb_number:
                continue
            # Есть ли inbox-msg упоминающий эту HAWB?
            cond = Q(hawb=h) | Q(raw_xml__icontains=h.hawb_number)
            has_msg = AltaInboxMessage.objects.filter(cond).exists()
            if has_msg:
                continue
            # Эта HAWB получила RELEASED ТОЛЬКО через propagation.
            if opts['dry_run']:
                if rolled < 20:
                    self.stdout.write(
                        f'  would rollback {h.hawb_number}  '
                        f'decl={h.customs_declaration_number}  '
                        f'release={h.release_date}')
                rolled += 1
                continue
            HouseWaybill.objects.filter(pk=h.pk).update(
                customs_status='',
                customs_declaration_number='',
                filed_date=None,
                release_date=None,
            )
            # Удаляем propagation-attempt
            HawbDeclarationAttempt.objects.filter(
                hawb=h, declaration_number=h.customs_declaration_number,
            ).delete()
            rolled += 1
        self.stdout.write(self.style.SUCCESS(f'Откачено: {rolled}'))
