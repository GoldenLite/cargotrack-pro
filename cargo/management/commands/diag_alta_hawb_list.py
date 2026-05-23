"""Быстрая сводка по списку HAWB-номеров.

Для каждого HAWB-номера показывает:
- В БД? Cargo, decl в БД
- Сколько inbox-сообщений содержат этот hawb_number в raw_xml
- Из них released/withdrawn (это потенциальные пропущенные релизы)

Запуск:
    uv run python manage.py diag_alta_hawb_list 10267530014 10267039841 ...
    # или из файла:
    uv run python manage.py diag_alta_hawb_list --from-file hawbs.txt
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, HouseWaybill


class Command(BaseCommand):
    help = 'Сводка по списку HAWB-номеров'

    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='*', default=[])
        parser.add_argument('--from-file', default='',
                            help='Путь к файлу с HAWB-номерами (по одному в строке)')

    def handle(self, *args, **opts):
        nums = list(opts['hawbs'])
        if opts['from_file']:
            nums.extend(
                line.strip()
                for line in Path(opts['from_file']).read_text(encoding='utf-8').splitlines()
                if line.strip()
            )
        if not nums:
            self.stdout.write(self.style.ERROR('Список пуст'))
            return

        self.stdout.write(f'{"HAWB":<14} {"cargo":<22} {"decl":<32} '
                          f'{"in_xml":<6} {"released":<8}')
        for hn in nums:
            h = (HouseWaybill.objects
                 .select_related('mawb')
                 .filter(hawb_number__iexact=hn)
                 .first())
            if not h:
                self.stdout.write(f'{hn:<14} NOT_IN_DB')
                continue
            cargo_num = h.mawb.awb_number if h.mawb else '—'
            decl = h.customs_declaration_number or ''

            in_xml = AltaInboxMessage.objects.filter(
                raw_xml__icontains=hn).count()
            released = AltaInboxMessage.objects.filter(
                raw_xml__icontains=hn,
                msg_kind__in=('released', 'withdrawn'),
            ).count()

            tag = ''
            if released > 0 and not decl:
                tag = '  ← релиз есть, ДТ не проставлена'
            elif in_xml > 0 and released == 0 and not decl:
                tag = '  ← сообщения есть, но release ещё не выпущен'

            self.stdout.write(
                f'{hn:<14} {cargo_num:<22} {decl:<32} '
                f'{in_xml:<6} {released:<8}{tag}'
            )
