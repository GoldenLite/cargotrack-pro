"""Синхронизация ТРАНЗИТНЫХ партий с фактической постановкой на НАШ СВХ.

Косяк, который чинит (кейс 555-15321062 / транзит 130826-3, 17.08.2026): у груза,
пришедшего к нам ТРАНЗИТОМ, свой ДО1 (CMN.13010) часто не приходит вовсе, а
`scan_into_bond` остаётся РАННЕЙ авиа-датой (до транзита). Тогда в «Общее»
«дата прибытия» и «дата регистрации ДО1» показывают авиа-дату, а лицензия/
рег.номер/ТСД уже транзитные → рассинхрон «половина от авиа, половина от партии».

Что делает (идемпотентно, для партий с `transit_doc` на нашем Внуково):
1. `scan_into_bond` ← дата регистрации НАШЕЙ описи (CMN.13029) — сдвиг ТОЛЬКО
   ВПЕРЁД (наша постановка на СВХ после транзита позже авиа-даты; назад не двигаем).
2. Writeback «Общее»:
   - лицензия / дата рег.ДО1 / рег.номер ДО1 (batch_write_svh_for_cargos);
   - «дата прибытия» ← дата размещения, ПЕРЕТИРАЯ (для транзита — по решению юзера);
   - ТСД ← transit_doc, перебивая авиа-номер партии.

Настоящий ДО1, если придёт позже, всё равно перепишет дату (apply_svh_do1 override).

    manage.py reconcile_transit_placement                 # dry-run
    manage.py reconcile_transit_placement --apply
    manage.py reconcile_transit_placement --mawb 555-15321062 --apply
"""
from datetime import datetime, time

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_date

from cargo.models import Cargo, AltaInboxMessage
from cargo.services.alta.inbox import OUR_WAREHOUSE_LICENSE
from cargo.services.sheets.writeback import (
    batch_write_svh_for_cargos,
    write_transit_arrival_for_cargos,
    write_transit_tsd_for_cargos,
)


class Command(BaseCommand):
    help = ('Синхронизировать транзитные партии (дата размещения / ТСД / '
            'дата прибытия) с датой нашей описи СВХ.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Реально применить (без флага — dry-run)')
        parser.add_argument('--mawb', default='', help='Только эта партия (awb_number)')

    def _opis_reg_date(self, cargo):
        opis = (AltaInboxMessage.objects
                .filter(cargo=cargo, msg_type='CMN.13029', msg_kind='svh_placed')
                .order_by('-prepared_at').first())
        if not opis:
            return None
        pm = opis.parsed_meta or {}
        rd = (pm.get('registration_date') or pm.get('svh_presentation_date')
              or pm.get('svh_inventory_instance_date') or '').strip()
        return parse_date(rd) if rd else None

    def handle(self, *args, **o):
        apply = o['apply']
        qs = Cargo.objects.filter(transit_doc__gt='')
        if o['mawb']:
            qs = qs.filter(awb_number=o['mawb'])

        confirmed = []     # транзиты с НАЙДЕННОЙ описью → безопасно писать в лист
        scan_moved = 0
        no_opis = 0
        for c in qs.iterator():
            if (c.warehouse_license or '').strip() != OUR_WAREHOUSE_LICENSE:
                continue   # не наш Внуково — не трогаем
            reg_date = self._opis_reg_date(c)
            if not reg_date:
                # Без нашей описи scan_into_bond может быть стухшей авиа-датой —
                # НЕ перетираем лист (иначе можно откатить ручную правку).
                no_opis += 1
                self.stdout.write(
                    f'  {c.awb_number} (transit {c.transit_doc}): нет описи — пропуск')
                continue
            new_dt = datetime.combine(reg_date, time(12, 0))
            if timezone.is_naive(new_dt):
                new_dt = timezone.make_aware(new_dt, timezone.get_current_timezone())
            cur = c.scan_into_bond
            # Сравнение в ЛОКАЛИ (scan_into_bond в БД — UTC; иначе даты у границы
            # суток сравнивались бы криво).
            cur_local = timezone.localtime(cur).date() if cur else None
            if cur is None or new_dt.date() > cur_local:
                note = f'scan {cur_local} -> {new_dt.date()}'
                if apply:
                    Cargo.objects.filter(pk=c.pk).update(scan_into_bond=new_dt)
                c.scan_into_bond = new_dt
                scan_moved += 1
            else:
                note = 'date ok'
            confirmed.append(c)
            self.stdout.write(f'  {c.awb_number} (transit {c.transit_doc}): {note}')

        self.stdout.write(
            f'Транзитных партий с описью: {len(confirmed)}, дата сдвинута: '
            f'{scan_moved}, без описи (пропущено): {no_opis}')

        if not confirmed:
            return
        if not apply:
            self.stdout.write('DRY-RUN: writeback пропущен (нужен --apply).')
            return

        n1 = batch_write_svh_for_cargos(confirmed)
        n2 = write_transit_arrival_for_cargos(confirmed)
        n3 = write_transit_tsd_for_cargos(confirmed)
        self.stdout.write(f'writeback: svh={n1} arrival={n2} tsd={n3}')
