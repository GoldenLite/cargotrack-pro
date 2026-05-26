"""Backfill вес/места ДО1 из всех известных ED.DO1.

Идёт по AltaOutboxObservation(msg_type='ED.DO1'), для каждого
HAWB в parsed_meta['goods']:
  - если HouseWaybill.svh_do1_gross_weight пуст/0 → запишет вес;
  - если svh_do1_place_count пуст/0 → запишет места;
  - если оба поля уже заполнены — НЕ перезаписывает (юзер: «не заполняем
    где уже есть»);
Запускает writeback на затронутые HAWB (и заодно на тех, у кого БД
заполнена, а в Sheets ячейки могут быть пустыми).

Запуск:
    python manage.py backfill_do1_goods
    python manage.py backfill_do1_goods --dry-run
    python manage.py backfill_do1_goods --force-writeback   # перезапишет
                              # Sheets даже если в БД ничего не изменилось
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand

from cargo.models import AltaOutboxObservation, HouseWaybill


class Command(BaseCommand):
    help = 'Дозаполнить svh_do1_gross_weight/svh_do1_place_count из ED.DO1'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--force-writeback', action='store_true',
                            help='Триггерить Sheets writeback даже если в БД '
                                 'ничего не изменилось (стейл-ячейки).')

    def handle(self, *args, **opts):
        qs = (AltaOutboxObservation.objects
              .filter(msg_type='ED.DO1')
              .order_by('-prepared_at'))
        total = qs.count()
        self.stdout.write(f'AltaOutboxObservation ED.DO1: {total}')

        seen_hawbs: set[str] = set()
        touched_for_writeback: list[HouseWaybill] = []
        wrote_weight = 0
        wrote_places = 0
        skipped_full = 0
        missing_hawb = 0

        for obs in qs.iterator():
            goods = (obs.parsed_meta or {}).get('goods') or {}
            for hawb_num, data in goods.items():
                # Берём самый свежий ED.DO1 для каждой HAWB; остальные пропуск.
                if hawb_num in seen_hawbs:
                    continue
                seen_hawbs.add(hawb_num)

                h = HouseWaybill.objects.filter(
                    hawb_number__iexact=hawb_num).first()
                if not h:
                    missing_hawb += 1
                    continue

                try:
                    src_weight = Decimal(str(data.get('weight') or '0'))
                except (InvalidOperation, ValueError):
                    src_weight = None
                src_places = data.get('places') or None

                upd: dict = {}
                # only-missing: пишем только если поле пустое/None/0
                if (src_weight and src_weight > 0 and
                        not (h.svh_do1_gross_weight and h.svh_do1_gross_weight > 0)):
                    upd['svh_do1_gross_weight'] = src_weight
                    wrote_weight += 1
                if (src_places and not (h.svh_do1_place_count and h.svh_do1_place_count > 0)):
                    upd['svh_do1_place_count'] = src_places
                    wrote_places += 1

                if not upd:
                    skipped_full += 1
                    if opts['force_writeback']:
                        touched_for_writeback.append(h)
                    continue

                if opts['dry_run']:
                    self.stdout.write(
                        f'  [DRY] {hawb_num}: {upd}'
                    )
                else:
                    HouseWaybill.objects.filter(pk=h.pk).update(**upd)
                    touched_for_writeback.append(h)

        self.stdout.write(self.style.SUCCESS(
            f'\nweights filled: {wrote_weight} | places filled: {wrote_places} | '
            f'fully-populated skipped: {skipped_full} | hawb-missing: {missing_hawb}'
        ))

        if opts['dry_run']:
            return

        # Writeback в Sheets для всех затронутых
        if touched_for_writeback:
            try:
                from cargo.services.sheets.writeback import (
                    batch_write_svh_do1_weight_for_hawbs,
                    batch_write_svh_do1_places_for_hawbs,
                )
                for h in touched_for_writeback:
                    h.refresh_from_db(
                        fields=['svh_do1_gross_weight', 'svh_do1_place_count'])
                w = batch_write_svh_do1_weight_for_hawbs(touched_for_writeback)
                p = batch_write_svh_do1_places_for_hawbs(touched_for_writeback)
                self.stdout.write(self.style.SUCCESS(
                    f'Sheets writeback: weight cells={w}, places cells={p}'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f'writeback failed: {e}'))
