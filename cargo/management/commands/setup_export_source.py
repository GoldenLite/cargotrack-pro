"""Создать/обновить SheetSource(kind='export') для вкладки «Экспортная статистика».

Использует spreadsheet_id той же таблицы что и general-источник (по умолчанию)
или принимает через --spreadsheet-id.

Запуск:
    uv run python manage.py setup_export_source
    uv run python manage.py setup_export_source --tab-name="Экспортная статистика"
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import SheetSource


class Command(BaseCommand):
    help = 'Создаёт/обновляет SheetSource(kind=export) для вкладки экспорта'

    def add_arguments(self, parser):
        parser.add_argument('--spreadsheet-id', default='',
                            help='ID Google-таблицы (по умолчанию = у general-источника)')
        parser.add_argument('--tab-name', default='Экспортная статистика',
                            help='Имя вкладки')
        parser.add_argument('--name', default='Экспортная статистика',
                            help='Произвольное имя SheetSource')
        parser.add_argument('--header-row', type=int, default=1,
                            help='Номер строки шапки')

    def handle(self, *args, **opts):
        ssid = opts['spreadsheet_id']
        if not ssid:
            gen = SheetSource.objects.filter(kind='general',
                                             is_active=True).first()
            if not gen:
                self.stdout.write(self.style.ERROR(
                    'Нет активного general-источника. Укажите --spreadsheet-id.'
                ))
                return
            ssid = gen.spreadsheet_id
            self.stdout.write(f'Используем spreadsheet_id от general: {ssid}')

        src, created = SheetSource.objects.update_or_create(
            kind='export',
            tab_name=opts['tab_name'],
            spreadsheet_id=ssid,
            defaults={
                'name':       opts['name'],
                'header_row': opts['header_row'],
                'is_active':  True,
            },
        )
        action = 'создан' if created else 'обновлён'
        self.stdout.write(self.style.SUCCESS(
            f'SheetSource {action}: id={src.pk} kind={src.kind} '
            f'tab={src.tab_name} sheet={src.spreadsheet_id}'
        ))
