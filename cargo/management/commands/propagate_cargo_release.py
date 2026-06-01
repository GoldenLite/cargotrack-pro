"""Cargo-level пропагация decl+release+status.

Сценарий: у партии большинство HAWB released (одной ДТ), но
несколько HAWB остались пустыми — их не было в raw_xml CMN.11350
или они добавились к партии позже. Если в одной партии >50% HAWB
имеют одинаковый decl + release, копируем эти значения остальным.
"""
from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand

from cargo.models import Cargo, HawbDeclarationAttempt, HouseWaybill


class Command(BaseCommand):
    help = 'Cargo-level пропагация decl/release HAWB-siblings.'

    def add_arguments(self, parser):
        parser.add_argument('--cargo', help='Только эта партия (pk or awb)')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        from cargo.services.sheets.writeback import (
            begin_batch_writeback, end_batch_writeback,
        )

        if opts['cargo']:
            arg = opts['cargo']
            if arg.isdigit():
                cargos = Cargo.objects.filter(pk=int(arg))
            else:
                cargos = Cargo.objects.filter(awb_number=arg)
        else:
            # Все партии у которых есть и released, и пустые HAWB
            cargos = Cargo.objects.filter(
                hawbs__customs_status='RELEASED',
            ).distinct()

        n_propagated_total = 0
        cargos_touched = 0

        for cargo in cargos:
            hawbs = list(cargo.hawbs.all())
            released = [h for h in hawbs if h.customs_status == 'RELEASED'
                        and (h.customs_declaration_number or '').strip()
                        and h.release_date]
            empty = [h for h in hawbs if h.customs_status == ''
                     and not (h.customs_declaration_number or '').strip()
                     and not h.release_date]
            if not empty or not released:
                continue

            # Доминирующая (decl, release_date) среди released.
            decl_release_counts = Counter(
                (h.customs_declaration_number, h.release_date)
                for h in released)
            (decl, release_dt), n = decl_release_counts.most_common(1)[0]
            # Принимаем доминирующий только если он покрывает >50% released.
            if n * 2 < len(released):
                continue

            self.stdout.write(
                f'Cargo {cargo.awb_number} (pk={cargo.pk}): '
                f'released={len(released)} empty={len(empty)} '
                f'dominant_decl={decl!r}')

            filed_dt = None
            for h in released:
                if h.customs_declaration_number == decl and h.filed_date:
                    filed_dt = h.filed_date
                    break

            if opts['dry_run']:
                for h in empty[:5]:
                    self.stdout.write(f'  would set {h.hawb_number} → '
                                      f'decl={decl} release={release_dt}')
                cargos_touched += 1
                n_propagated_total += len(empty)
                continue

            begin_batch_writeback()
            try:
                for h in empty:
                    # Обновляем поля HouseWaybill напрямую (минуя save() rules)
                    HouseWaybill.objects.filter(pk=h.pk).update(
                        customs_declaration_number=decl,
                        filed_date=filed_dt,
                        release_date=release_dt,
                        customs_status='RELEASED',
                    )
                    # Создаём attempt RELEASED
                    HawbDeclarationAttempt.objects.update_or_create(
                        hawb=h, declaration_number=decl,
                        defaults={
                            'status': 'RELEASED',
                            'filed_date': filed_dt,
                            'release_date': release_dt,
                            'attempt_number': 1,
                        },
                    )
                    n_propagated_total += 1
            finally:
                end_batch_writeback()
            cargos_touched += 1

        self.stdout.write(self.style.SUCCESS(
            f'\nGотово. cargos_touched={cargos_touched}, '
            f'HAWB_propagated={n_propagated_total}'))
