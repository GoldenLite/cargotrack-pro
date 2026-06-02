"""Mark HAWB as RELEASED with explicit decl numbers.

Для каждой пары hawb:decl:
1. Set customs_declaration_number = decl
2. Parse release_date из decl format XXXXXXXX/DDMMYY/XXXXXXX
3. Set customs_status='RELEASED', release_date
4. Create/update HawbDeclarationAttempt

Использование (формат: "HAWB DECL" по строке, любой разделитель):

    manage.py mark_explicit_released --file pairs.txt
    manage.py mark_explicit_released --pairs "10242102579 10005020/180526/0017915,10261223951 10702020/010626/0019801"
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from django.core.management.base import BaseCommand

from cargo.models import HawbDeclarationAttempt, HouseWaybill


DECL_RE = re.compile(r'^(\d{8,9})/(\d{2})(\d{2})(\d{2})/([^\s]+)$')


def _parse_decl_date(decl: str) -> datetime | None:
    m = DECL_RE.match(decl.strip())
    if not m:
        return None
    _, dd, mm, yy = m.group(1), m.group(2), m.group(3), m.group(4)
    try:
        return datetime(2000 + int(yy), int(mm), int(dd), 12, 0,
                        tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_pairs(text: str) -> list[tuple[str, str]]:
    """Парсит формат '10242102579 - 10005020/180526/0017915' (или
    с любыми разделителями)."""
    out = []
    for line in text.replace(',', '\n').splitlines():
        line = line.strip()
        if not line:
            continue
        # Ищем HAWB (10-11 цифр) и decl (8-9цифр/6цифр/что-то)
        m = re.search(r'(\d{10,11})\D+(\d{8,9}/\d{6}/[^\s]+)', line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--file', help='Файл с парами hawb decl')
        parser.add_argument('--pairs', help='CSV пар: "HAWB DECL,HAWB DECL"')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        text = ''
        if opts['file']:
            with open(opts['file'], encoding='utf-8') as f:
                text = f.read()
        if opts['pairs']:
            text += '\n' + opts['pairs']

        pairs = _parse_pairs(text)
        self.stdout.write(f'Pairs: {len(pairs)}')

        n_ok = 0
        n_skip_nohawb = 0
        n_skip_baddt = 0
        for hn, decl in pairs:
            h = HouseWaybill.objects.filter(hawb_number=hn).first()
            if not h:
                self.stdout.write(f'  skip {hn}: not in DB')
                n_skip_nohawb += 1
                continue
            release_dt = _parse_decl_date(decl)
            if not release_dt:
                self.stdout.write(f'  skip {hn}: bad decl {decl!r}')
                n_skip_baddt += 1
                continue

            self.stdout.write(
                f'  {hn} → decl={decl} release={release_dt.date()}')

            if opts['dry_run']:
                n_ok += 1
                continue

            HouseWaybill.objects.filter(pk=h.pk).update(
                customs_status='RELEASED',
                customs_declaration_number=decl,
                release_date=release_dt,
            )
            HawbDeclarationAttempt.objects.update_or_create(
                hawb=h, declaration_number=decl,
                defaults={
                    'status': 'RELEASED',
                    'release_date': release_dt,
                    'attempt_number': 1,
                },
            )
            n_ok += 1

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. ok={n_ok} skip_nohawb={n_skip_nohawb} '
            f'skip_baddt={n_skip_baddt}'))
