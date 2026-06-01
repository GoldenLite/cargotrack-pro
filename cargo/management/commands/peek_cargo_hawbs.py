"""Список HAWB партии с их customs-state."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import Cargo


class Command(BaseCommand):
    help = 'HAWB партии с customs-state.'

    def add_arguments(self, parser):
        parser.add_argument('cargo_pk_or_awb')

    def handle(self, *args, **opts):
        arg = opts['cargo_pk_or_awb']
        if arg.isdigit():
            cargo = Cargo.objects.filter(pk=int(arg)).first()
        else:
            cargo = Cargo.objects.filter(awb_number=arg).first()
        if not cargo:
            self.stdout.write(f'Cargo {arg} не найден')
            return
        self.stdout.write(
            f'Cargo pk={cargo.pk} awb={cargo.awb_number}')
        hawbs = list(cargo.hawbs.all().order_by('hawb_number'))
        self.stdout.write(f'HAWB: {len(hawbs)}')
        for h in hawbs:
            self.stdout.write(
                f'  {h.hawb_number}  '
                f'status={h.customs_status!r}  '
                f'decl={h.customs_declaration_number!r}  '
                f'release={h.release_date}')
