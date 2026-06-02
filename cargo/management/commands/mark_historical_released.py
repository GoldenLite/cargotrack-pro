"""Пометить как RELEASED все HAWB у которых есть customs_declaration_number
но customs_status='' и release_date=None.

Эти HAWB — историческое наследие: decl подгружен из Sheets до того как
мы начали собирать CMN.11350 из Альты. У них нет inbox-msg, поэтому
compute_ed_status возвращает пусто и incremental ничего не пишет.

Команда:
1. Находит таких HAWB.
2. Парсит дату из decl (формат XXXXXXXX/DDMMYY/XXXXXXX → release_date).
3. Set customs_status='RELEASED', release_date.
4. Создаёт HawbDeclarationAttempt с status='RELEASED' для consistency.

После этого compute_ed_status вернёт «Выпуск разрешен», incremental
напишет status/T/hide в Sheets автоматически.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from django.core.management.base import BaseCommand

from cargo.models import CrmHawbIndex, HawbDeclarationAttempt, HouseWaybill


# 8-9 цифр customs code (некоторые ED-обмена шифры идут как 9 цифр).
DECL_RE = re.compile(r'^(\d{8,9})/(\d{2})(\d{2})(\d{2})/([^\s]+)$')


def _parse_decl_date(decl: str) -> datetime | None:
    """10001020/130426/0013328 → datetime(2026, 4, 13)."""
    m = DECL_RE.match(decl.strip())
    if not m:
        return None
    _, dd, mm, yy = m.group(1), m.group(2), m.group(3), m.group(4)
    try:
        year = 2000 + int(yy)
        return datetime(year, int(mm), int(dd), 12, 0,
                        tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--limit', type=int, default=0,
                            help='Safety: ограничить кол-во (0 = no limit)')
        parser.add_argument('--all-db', action='store_true',
                            help='Не фильтровать по CrmHawbIndex (default: '
                                 'только HAWB на CRM-вкладках)')

    def handle(self, *args, **opts):
        qs = (HouseWaybill.objects
              .exclude(customs_declaration_number='')
              .filter(customs_status='', release_date__isnull=True))

        if not opts['all_db']:
            crm_hawbs = set(CrmHawbIndex.objects.values_list(
                'hawb_number', flat=True).distinct())
            self.stdout.write(f'HAWB на CRM-вкладках: {len(crm_hawbs)}')
            qs = qs.filter(hawb_number__in=crm_hawbs)

        total = qs.count()
        self.stdout.write(f'Кандидатов: {total}')

        if opts['limit']:
            qs = qs[:opts['limit']]

        n_updated = 0
        n_attempts = 0
        n_skipped = 0
        for h in qs.iterator(chunk_size=500):
            decl = (h.customs_declaration_number or '').strip()
            release_dt = _parse_decl_date(decl)
            if not release_dt:
                n_skipped += 1
                if n_skipped <= 10:
                    self.stdout.write(
                        f'  skip {h.hawb_number}: '
                        f'не парсится decl={decl!r}')
                continue

            if opts['dry_run']:
                n_updated += 1
                if n_updated <= 20:
                    self.stdout.write(
                        f'  would mark {h.hawb_number} '
                        f'decl={decl} release={release_dt.date()}')
                continue

            # 1) Update HAWB
            HouseWaybill.objects.filter(pk=h.pk).update(
                customs_status='RELEASED',
                release_date=release_dt,
            )
            # 2) Create HawbDeclarationAttempt
            _, created = HawbDeclarationAttempt.objects.update_or_create(
                hawb=h, declaration_number=decl,
                defaults={
                    'status': 'RELEASED',
                    'release_date': release_dt,
                    'attempt_number': 1,
                },
            )
            if created:
                n_attempts += 1
            n_updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. updated={n_updated} attempts_created={n_attempts} '
            f'skipped={n_skipped}'))
