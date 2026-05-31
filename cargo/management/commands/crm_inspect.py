"""Разведка CRM-таблицы: список специалистов + tabs + сводка строк по HAWB."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.services.sheets.client import get_client


CRM_ID = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'


class Command(BaseCommand):
    help = 'Разведка CRM-таблицы.'

    def handle(self, *args, **opts):
        client = get_client()
        ss = client.open_by_key(CRM_ID)
        self.stdout.write(f'Spreadsheet: {ss.title}')

        # Список сотрудников
        emp = ss.worksheet('Сотрудники')
        rows = emp.get_all_values()
        specialists = set()
        self.stdout.write('\n=== Сотрудники ===')
        for r in rows[1:]:
            if len(r) >= 2 and r[1].strip():
                specialists.add(r[1].strip())
                self.stdout.write(f'  {r[0]}: {r[1]}')

        self.stdout.write('\n=== Все tabs ===')
        all_tabs = ss.worksheets()
        specialist_tabs = []
        system_tabs = []
        for ws in all_tabs:
            kind = 'spec' if ws.title in specialists else 'sys'
            self.stdout.write(
                f'  [{kind}] {ws.title}  ({ws.row_count}x{ws.col_count})')
            if kind == 'spec':
                specialist_tabs.append(ws.title)
            else:
                system_tabs.append(ws.title)

        self.stdout.write(
            f'\nИтого specialist-tabs: {len(specialist_tabs)},  '
            f'system-tabs: {len(system_tabs)}')
