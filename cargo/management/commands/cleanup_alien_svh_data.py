"""Очищает SVH-поля у Cargo с лицензией ЧУЖОГО склада.

В БД могли остаться записи от старых периодов когда в inbox.classify ещё
не было фильтра по OUR_WAREHOUSE_LICENSE. Например партии 555-* которые
едут в «Москва Карго» (10005/...) имеют warehouse_license с чужой лицензией,
scan_into_bond и svh_do1_reg_number — наследие старого кода.

Reparse эти поля НЕ откатывает (apply_svh_do1 только set, не clear).
Эта команда — разовая очистка.

После очистки запускает Sheets resync — пустые ячейки в:
  CargoTrack: лицензия СВХ
  CargoTrack: дата регистрации ДО1 (scan_into_bond)
  CargoTrack: рег. номер ДО1

Запуск:
    uv run python manage.py cleanup_alien_svh_data --dry-run
    uv run python manage.py cleanup_alien_svh_data
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import Cargo
from cargo.services.alta.inbox import OUR_WAREHOUSE_LICENSE


class Command(BaseCommand):
    help = 'Очищает SVH-поля у Cargo с чужой warehouse_license'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        # Найти все Cargo где warehouse_license задан и НЕ наша
        affected = list(Cargo.objects.exclude(
            warehouse_license=''
        ).exclude(
            warehouse_license=OUR_WAREHOUSE_LICENSE
        ))

        self.stdout.write(
            f'Найдено {len(affected)} Cargo с чужой лицензией СВХ '
            f'(наша = {OUR_WAREHOUSE_LICENSE!r})'
        )

        # Группировка по лицензии для наглядности
        from collections import Counter
        lic_counter = Counter()
        for c in affected:
            lic_counter[c.warehouse_license] += 1
        self.stdout.write('Лицензии:')
        for lic, n in lic_counter.most_common():
            self.stdout.write(f'  {lic!r}: {n} партий')

        if not affected:
            return

        if opts['dry_run']:
            self.stdout.write('')
            self.stdout.write('--- DRY RUN — первые 10 партий ---')
            for c in affected[:10]:
                self.stdout.write(
                    f'  {c.awb_number}: lic={c.warehouse_license!r} '
                    f'scan_into_bond={c.scan_into_bond} '
                    f'svh_do1_reg_number={c.svh_do1_reg_number!r}'
                )
            return

        # Очистка SVH-полей. Прямой UPDATE минуя save() — модель Cargo может
        # делать своё в save(), не хотим побочных эффектов.
        pks = [c.pk for c in affected]
        Cargo.objects.filter(pk__in=pks).update(
            warehouse_license='',
            scan_into_bond=None,
            svh_do1_reg_number='',
        )
        self.stdout.write(
            self.style.SUCCESS(f'Очищено: {len(pks)} Cargo')
        )

        # Также: HouseWaybill.svh_do1_sent_at — но эти поля ставились через
        # ED.DO1 от НАШЕГО склада. Если для чужой партии стоит svh_do1_sent_at
        # — это ошибка матчинга, надо тоже очистить. Но не у всех HAWB
        # партии — только тем чьи Cargo сейчас были очищены.
        from cargo.models import HouseWaybill
        n_hawb = HouseWaybill.objects.filter(
            mawb_id__in=pks,
            svh_do1_sent_at__isnull=False,
        ).update(
            svh_do1_sent_at=None,
            svh_do1_gross_weight=None,
            svh_do1_place_count=None,
        )
        if n_hawb:
            self.stdout.write(
                self.style.SUCCESS(f'Очищено HAWB-полей: {n_hawb}')
            )

        # Sheets resync — пишем ОБНУЛЁННЫЕ значения для этих Cargo и их HAWB
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE('Sheets resync...'))
        try:
            from cargo.services.sheets.writeback import (
                batch_write_svh_for_cargos,
                batch_write_svh_do1_sent_for_hawbs,
                batch_write_svh_do1_weight_for_hawbs,
                batch_write_svh_do1_places_for_hawbs,
            )
            # Перечитать обновлённые Cargo
            cargos_fresh = list(Cargo.objects.filter(pk__in=pks))
            n = batch_write_svh_for_cargos(cargos_fresh)
            self.stdout.write(f'  svh (cargo-level): {n} cells')

            hawbs_fresh = list(HouseWaybill.objects.filter(mawb_id__in=pks))
            if hawbs_fresh:
                n = batch_write_svh_do1_sent_for_hawbs(hawbs_fresh)
                self.stdout.write(f'  svh_do1_sent: {n} cells ({len(hawbs_fresh)} HAWB)')
                n = batch_write_svh_do1_weight_for_hawbs(hawbs_fresh)
                self.stdout.write(f'  svh_do1_weight: {n} cells')
                n = batch_write_svh_do1_places_for_hawbs(hawbs_fresh)
                self.stdout.write(f'  svh_do1_places: {n} cells')
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'resync failed: {e}'))
