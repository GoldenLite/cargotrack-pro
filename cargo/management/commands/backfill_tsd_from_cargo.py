"""Sweeper: проставляет номер партии (Cargo.awb_number) в ПУСТУЮ колонку
«ТСД» таблицы «Общее» для HAWB, чья партия уже известна из СВХ.

ПРОБЛЕМА: колонку «CargoTrack: номер партии» удалили (26.05.2026) с расчётом
«MAWB и так виден в ТСД». Но ТСД — РУЧНАЯ колонка специалиста, и для партий,
рождённых из СВХ (adopt_svh_orphans / match_svh_do1 svh_mawb-ветка / ED.DO1
привязка), номер партии в ТСД никто не вводит. Итог: в «Общее» стоит ДО1
(лицензия/дата/рег.номер — они пишутся из Cargo), а какая партия — «непонятно».

РЕШЕНИЕ (запрос юзера 16.07.2026): взять awb_number партии, к которой HAWB
уже привязана, и положить его в ТСД. ТСД — и есть колонка партии (формат
MAWB), из неё же промоут создаёт Cargo (awb_number = ТСД) — запись обратная,
консистентная.

СТРАХОВКИ (все в writeback.batch_write_tsd_from_mawb_for_hawbs):
- пишем ТОЛЬКО в ПУСТУЮ ячейку ТСД (не перетираем ручной ввод);
- зеркалим в HAWB.tsd_number (иначе matcher видит diff «лист≠БД»);
- sort-proof (ряд из живой колонки «Накладная СДЭК»);
- колонку ТСД только находим, не создаём.

Кандидаты: shipment=IMPORT, привязка к партии есть, у Cargo проставлена
лицензия СВХ ИЛИ рег.номер ДО1 (= партия реально прошла СВХ), ТСД(БД) пусто,
awb_number непустой.

Идемпотентно, durable — можно кроном:
    manage.py backfill_tsd_from_cargo                 # dry-run (превью)
    manage.py backfill_tsd_from_cargo --apply
    manage.py backfill_tsd_from_cargo --apply --limit 50
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from cargo.models import HouseWaybill


class Command(BaseCommand):
    help = ('Проставляет номер партии (Cargo.awb_number) в пустую колонку ТСД '
            '«Общее» для HAWB с известной из СВХ партией.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Реально писать (без флага — dry-run превью)')
        parser.add_argument('--limit', type=int, default=0,
                            help='Максимум HAWB за прогон (0 = все)')

    def handle(self, *args, **opts):
        apply = opts['apply']
        limit = opts['limit']

        qs = (HouseWaybill.objects
              .filter(shipment_type='IMPORT')
              .exclude(mawb__isnull=True)
              .filter(Q(mawb__warehouse_license__gt='')
                      | Q(mawb__svh_do1_reg_number__gt=''))
              .filter(Q(tsd_number='') | Q(tsd_number__isnull=True))
              .exclude(mawb__awb_number='')
              .select_related('mawb')
              .order_by('hawb_number'))
        if limit:
            qs = qs[:limit]

        hawbs = list(qs)
        self.stdout.write(f'Кандидатов (партия известна, ТСД пусто): {len(hawbs)}')
        if not hawbs:
            self.stdout.write(self.style.SUCCESS('Нечего делать.'))
            return

        from cargo.services.sheets.writeback import (
            batch_write_tsd_from_mawb_for_hawbs,
        )
        n_written, planned = batch_write_tsd_from_mawb_for_hawbs(
            hawbs, dry_run=not apply)

        # planned учитывает живой ряд + пустоту ячейки в листе → это реальный
        # объём (кандидаты по БД могут быть шире: HAWB нет в листе сейчас, или
        # ячейка ТСД уже занята ручным вводом, не отражённым в БД-поле).
        self.stdout.write(f'К записи (ячейка ТСД пуста, ряд в листе есть): '
                          f'{len(planned)}')
        for hn, awb, row in planned[:25]:
            self.stdout.write(f'  {hn} → ТСД={awb}  (ряд {row})')
        if len(planned) > 25:
            self.stdout.write(f'  … ещё {len(planned) - 25}')

        if apply:
            self.stdout.write(self.style.SUCCESS(
                f'Записано ячеек ТСД: {n_written} '
                f'(+ зеркалено в HAWB.tsd_number).'))
        else:
            self.stdout.write(self.style.WARNING(
                'DRY-RUN — ничего не записано. Повторить с --apply.'))
