"""Аудит соответствия рег.номер ДТ ↔ накладная (HAWB).

Для каждой HouseWaybill с непустым customs_declaration_number проверяет, есть
ли в наших Alta-сообщениях (inbox CMN.* + outbox observations) сообщение с
ЭТИМ ЖЕ рег.номером ДТ, в котором НАЗВАНА именно эта накладная. Источники
имён HAWB в сообщении: consignments[].waybills, providing_hawbs, hawbs,
waybill_number_raw, а для флагов — fallback по raw_xml (тело сообщения).

Категории:
  VERIFIED  — рег.номер подтверждён сообщением, называющим эту накладную.
  EXTERNAL  — рег.номер вообще НЕ встречается в наших сообщениях → ручной /
              клиентский ввод (растаможка мимо нашей Alta). Сверять нечего.
  MISMATCH  — рег.номер ЕСТЬ в сообщениях, но они называют ДРУГИЕ накладные,
              а не эту (и его нет в raw_xml тех сообщений) → подозрение на
              перетекание decl не на ту накладную.

Запуск:
    manage.py audit_decl_hawb_match
    manage.py audit_decl_hawb_match --examples 30
    manage.py audit_decl_hawb_match --no-raw-check     # быстрее, без 2-го прохода
    manage.py audit_decl_hawb_match --csv mismatch.csv
"""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand

from cargo.models import (
    HouseWaybill, AltaInboxMessage, AltaOutboxObservation,
)
from cargo.services.alta.inbox import _build_declaration_number


def _named_hawbs(pm: dict) -> set:
    """HAWB-номера, явно названные в structured-полях сообщения."""
    out: set = set()
    for c in (pm.get('consignments') or []):
        for w in (c.get('waybills') or []):
            out.add(str(w).strip())
    for key in ('providing_hawbs', 'hawbs'):
        for w in (pm.get(key) or []):
            out.add(str(w).strip())
    return out


class Command(BaseCommand):
    help = 'Аудит соответствия рег.номер ДТ ↔ накладная (HAWB)'

    def add_arguments(self, parser):
        parser.add_argument('--examples', type=int, default=20,
                            help='Сколько MISMATCH-примеров показать')
        parser.add_argument('--no-raw-check', action='store_true',
                            help='Не делать 2-й проход по raw_xml (быстрее, '
                                 'но возможны ложные MISMATCH от raw-only связок)')
        parser.add_argument('--csv', default='',
                            help='Записать MISMATCH в CSV-файл')

    def handle(self, *args, **opts):
        # 1. decl -> {названные HAWB}, и decl -> [inbox msg ids] для raw-прохода
        decl_to_hawbs: dict[str, set] = defaultdict(set)
        decl_to_msgids: dict[str, list] = defaultdict(list)

        for m in AltaInboxMessage.objects.values(
                'id', 'parsed_meta', 'waybill_number_raw').iterator(chunk_size=2000):
            pm = m['parsed_meta'] or {}
            decl = _build_declaration_number(pm)
            if not decl:
                continue
            named = _named_hawbs(pm)
            wr = (m['waybill_number_raw'] or '').strip()
            if wr:
                named.add(wr)
            decl_to_hawbs[decl].update(named)
            decl_to_msgids[decl].append(m['id'])

        for m in AltaOutboxObservation.objects.values(
                'parsed_meta', 'waybill_number').iterator(chunk_size=2000):
            pm = m['parsed_meta'] or {}
            decl = _build_declaration_number(pm)
            if not decl:
                continue
            for hn in (pm.get('hawbs') or []):
                decl_to_hawbs[decl].add(str(hn).strip())
            wb = (m['waybill_number'] or '').strip()
            if wb:
                decl_to_hawbs[decl].add(wb)

        self.stdout.write(f'decl с привязкой к накладным: {len(decl_to_hawbs)}')

        # 2. проход по HAWB c decl
        verified = external = 0
        mismatch_rows: list[tuple] = []
        by_type = defaultdict(int)
        for h in (HouseWaybill.objects
                  .exclude(customs_declaration_number='')
                  .values('hawb_number', 'customs_declaration_number',
                          'shipment_type')
                  .iterator(chunk_size=2000)):
            decl = (h['customs_declaration_number'] or '').strip()
            if not decl:
                continue
            named = decl_to_hawbs.get(decl)
            if named is None:
                external += 1
                continue
            if h['hawb_number'] in named:
                verified += 1
            else:
                mismatch_rows.append(
                    (h['hawb_number'], decl, h['shipment_type'] or ''))

        # 3. 2-й проход: raw_xml (вдруг накладная упомянута в теле)
        if not opts['no_raw_check'] and mismatch_rows:
            kept = []
            for hn, decl, st in mismatch_rows:
                ids = decl_to_msgids.get(decl, [])
                if ids and AltaInboxMessage.objects.filter(
                        id__in=ids, raw_xml__icontains=hn).exists():
                    continue  # есть в raw_xml — легитимно
                kept.append((hn, decl, st))
            mismatch_rows = kept

        for _, _, st in mismatch_rows:
            by_type[st or '(пусто)'] += 1

        total = verified + external + len(mismatch_rows)
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=== РЕЗУЛЬТАТ ==='))
        self.stdout.write(f'  VERIFIED (подтверждён сообщением): {verified}')
        self.stdout.write(f'  EXTERNAL (вне Alta, ручной ввод)  : {external}')
        self.stdout.write(self.style.WARNING(
            f'  MISMATCH (decl не на той накладной): {len(mismatch_rows)}'))
        if by_type:
            self.stdout.write('    по типу: ' + ', '.join(
                f'{k}={v}' for k, v in by_type.items()))
        pct = (100 * len(mismatch_rows) / total) if total else 0
        self.stdout.write(f'  всего проверено: {total}  (MISMATCH {pct:.2f}%)')

        for hn, decl, st in mismatch_rows[:opts['examples']]:
            named = sorted(decl_to_hawbs.get(decl, set()))[:6]
            self.stdout.write(
                f'    {hn} [{st}] decl={decl} → сообщения называют {named}')

        if opts['csv'] and mismatch_rows:
            import csv
            with open(opts['csv'], 'w', newline='', encoding='utf-8') as f:
                w = csv.writer(f)
                w.writerow(['hawb_number', 'decl', 'shipment_type',
                            'decl_names_hawbs'])
                for hn, decl, st in mismatch_rows:
                    w.writerow([hn, decl, st,
                                ';'.join(sorted(decl_to_hawbs.get(decl, set())))])
            self.stdout.write(f'  CSV: {opts["csv"]} ({len(mismatch_rows)} строк)')
