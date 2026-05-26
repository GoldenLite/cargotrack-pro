"""Статистика CMN.13010 за период.

CMN.13010 несёт два разных события:
  FormReport=1 → регистрация ДО1 (то что нам нужно для svh_do1_reg_number)
  FormReport=2 → выпуск ДО2 со склада (этим занимается CMN.13014, дублирующая
                 информация, мы маркируем как info и не используем)

Команда показывает поток за N дней: сколько form=1, сколько form=2, по каким
лицензиям, сколько привязано к нашим Cargo.

Запуск:
    python manage.py cmn13010_stats --days 7
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from cargo.models import AltaInboxMessage
from cargo.services.alta.inbox import OUR_WAREHOUSE_LICENSE


class Command(BaseCommand):
    help = 'Статистика CMN.13010 по FormReport / лицензиям'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=7)

    def handle(self, *args, **opts):
        since = timezone.now() - timedelta(days=opts['days'])
        qs = AltaInboxMessage.objects.filter(
            msg_type='CMN.13010', received_at__gte=since,
        )
        total = qs.count()
        self.stdout.write(f'CMN.13010 за последние {opts["days"]} дней: {total}')

        # Распределение по form / kind / lic / cargo
        by_form = Counter()
        by_kind = Counter()
        by_lic_form1 = Counter()
        by_cargo_form1 = Counter()
        for m in qs.only('parsed_meta', 'msg_kind', 'cargo_id'):
            pm = m.parsed_meta or {}
            form = (pm.get('svh_do1_form_report') or '').strip() or '<missing>'
            lic  = (pm.get('svh_warehouse_license') or '').strip() or '<missing>'
            by_form[form] += 1
            by_kind[m.msg_kind] += 1
            if form == '1':
                by_lic_form1[lic] += 1
                by_cargo_form1['attached' if m.cargo_id else 'unattached'] += 1

        self.stdout.write('\nFormReport распределение:')
        for f, n in sorted(by_form.items(), key=lambda x: -x[1]):
            self.stdout.write(f'  form={f}: {n}')

        self.stdout.write('\nmsg_kind распределение:')
        for k, n in sorted(by_kind.items(), key=lambda x: -x[1]):
            self.stdout.write(f'  {k}: {n}')

        self.stdout.write(f'\nFormReport=1: лицензии (наша={OUR_WAREHOUSE_LICENSE}):')
        for lic, n in sorted(by_lic_form1.items(), key=lambda x: -x[1]):
            mark = ' ← наша' if lic == OUR_WAREHOUSE_LICENSE else ''
            self.stdout.write(f'  {lic}: {n}{mark}')

        self.stdout.write('\nFormReport=1: привязка к Cargo:')
        for k, n in by_cargo_form1.items():
            self.stdout.write(f'  {k}: {n}')

        # Топ-10 неприсоединённых form=1 нашей лицензии (вдруг есть что добить)
        unattached = list(AltaInboxMessage.objects.filter(
            msg_type='CMN.13010',
            received_at__gte=since,
            cargo__isnull=True,
            msg_kind='svh_do1_registered',
        ).order_by('-prepared_at')[:10])
        self.stdout.write(f'\nПоследние 10 form=1 нашей лицензии без cargo: {len(unattached)}')
        for m in unattached:
            pm = m.parsed_meta or {}
            self.stdout.write(
                f'  {m.prepared_at} | env={m.envelope_id} | '
                f'reg={pm.get("svh_do1_reg_number")!r}')
