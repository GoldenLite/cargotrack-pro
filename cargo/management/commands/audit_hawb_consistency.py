"""Аудит консистентности HouseWaybill — поиск странностей по всей БД.

Проверяет:
1. HAWB с данными СВХ/ДТ но без mawb_id (битая связка).
2. HAWB с release_date < filed_date.
3. HAWB с customs_status='RELEASED' но без customs_declaration_number.
4. HAWB с одной ДТ но в РАЗНЫХ Cargo (странно — одна ДТ должна быть в одной партии).
5. HAWB у Cargo с moscow-cargo префиксом, но HAWB имеет svh_do1_sent_at
   (мы ED.DO1 на Москва-Карго НЕ подаём — это их склад).
6. HAWB которых нет в Sheets «Общее» (orphan).

Запуск:
    uv run python manage.py audit_hawb_consistency
    uv run python manage.py audit_hawb_consistency -v   # с примерами
"""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill, ImportedSheetRow
from cargo.services.external_warehouse.applier import MOSCOW_CARGO_PREFIXES


def _is_mc_cargo(cargo) -> bool:
    if not cargo:
        return False
    awb = (cargo.awb_number or '').strip()
    return len(awb) >= 4 and awb[3] == '-' and awb[:3] in MOSCOW_CARGO_PREFIXES


class Command(BaseCommand):
    help = 'Аудит HouseWaybill — поиск битых связок и нестыковок'

    def add_arguments(self, parser):
        parser.add_argument('-v', '--verbose', action='count', default=0)

    def handle(self, *args, **opts):
        n_total = HouseWaybill.objects.count()
        self.stdout.write(f'HouseWaybill всего в БД: {n_total}')

        # 1. С СВХ-данными но без mawb
        no_mawb_with_data = list(HouseWaybill.objects.filter(
            mawb_id__isnull=True,
        ).exclude(
            svh_do1_sent_at__isnull=True,
            svh_do2_send_at__isnull=True,
            customs_declaration_number='',
            filed_date__isnull=True,
            release_date__isnull=True,
        ))
        self._report('1. С данными СВХ/ДТ/дат, но mawb_id=None (битая связка)',
                     no_mawb_with_data, opts,
                     fmt=lambda h: (
                         f'  {h.hawb_number}: '
                         f'sent_at={h.svh_do1_sent_at} '
                         f'do2={h.svh_do2_send_at} '
                         f'ДТ={h.customs_declaration_number!r} '
                         f'filed={h.filed_date} released={h.release_date}'
                     ))

        # 2. release_date < filed_date
        bad_times = []
        for h in HouseWaybill.objects.exclude(
            filed_date__isnull=True
        ).exclude(release_date__isnull=True).only(
            'hawb_number', 'filed_date', 'release_date'
        ):
            if h.release_date < h.filed_date:
                bad_times.append(h)
        self._report('2. release_date < filed_date', bad_times, opts,
                     fmt=lambda h: (
                         f'  {h.hawb_number}: filed={h.filed_date} '
                         f'released={h.release_date}'
                     ))

        # 3. RELEASED но без ДТ
        rel_no_decl = list(HouseWaybill.objects.filter(
            customs_status='RELEASED',
            customs_declaration_number='',
        ))
        self._report('3. customs_status=RELEASED но customs_declaration_number пуст',
                     rel_no_decl, opts,
                     fmt=lambda h: f'  {h.hawb_number}: mawb={h.mawb.awb_number if h.mawb else None}')

        # 4. Одна ДТ в разных Cargo
        decl_to_cargos: dict = defaultdict(set)
        for h in HouseWaybill.objects.exclude(
            customs_declaration_number=''
        ).only('hawb_number', 'customs_declaration_number', 'mawb_id'):
            if h.mawb_id:
                decl_to_cargos[h.customs_declaration_number].add(h.mawb_id)
        multi_cargo_decls = {d: cs for d, cs in decl_to_cargos.items() if len(cs) > 1}
        self._report('4. Одна ДТ в разных Cargo (multi-cargo declaration)',
                     list(multi_cargo_decls.items()), opts,
                     fmt=lambda x: f'  ДТ {x[0]}: Cargo IDs {x[1]}')

        # 5. moscow-cargo Cargo HAWB с нашим svh_do1_sent_at
        mc_hawbs_with_sent = list(HouseWaybill.objects.filter(
            svh_do1_sent_at__isnull=False,
            mawb__awb_number__regex=r'^(784|555|826|537|880)-',
        ).select_related('mawb'))
        self._report(
            '5. HAWB у Москва-Карго партии имеет наш svh_do1_sent_at '
            '(мы ED.DO1 на их склад не подаём!)',
            mc_hawbs_with_sent, opts,
            fmt=lambda h: (
                f'  {h.hawb_number} → {h.mawb.awb_number}: '
                f'sent_at={h.svh_do1_sent_at}'
            )
        )

        # 6. HAWB в БД но не в Sheets «Общее»
        in_sheets = set(
            ImportedSheetRow.objects.filter(source__kind='general')
            .values_list('hawb_number_norm', flat=True)
        )
        all_hawb_numbers = set(
            HouseWaybill.objects.values_list('hawb_number', flat=True)
        )
        not_in_sheets = all_hawb_numbers - in_sheets
        # фильтр пустых
        not_in_sheets = [n for n in not_in_sheets if n]
        self._report('6. HAWB в БД но НЕТ в Sheets «Общее»',
                     [(n,) for n in not_in_sheets], opts,
                     fmt=lambda t: f'  {t[0]}')

    def _report(self, label: str, items: list, opts: dict, fmt):
        self.stdout.write('')
        n = len(items)
        if n == 0:
            self.stdout.write(self.style.SUCCESS(f'{label}: 0'))
            return
        self.stdout.write(self.style.WARNING(f'{label}: {n}'))
        if opts['verbose']:
            limit = 50 if opts['verbose'] >= 2 else 10
            for it in items[:limit]:
                try:
                    self.stdout.write(fmt(it))
                except Exception as e:
                    self.stdout.write(f'  [fmt error: {e}]')
            if n > limit:
                self.stdout.write(f'  ... ещё {n-limit}')
