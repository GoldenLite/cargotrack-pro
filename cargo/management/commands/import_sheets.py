"""Импорт Google Sheets → ImportedSheetRow + HawbWorkflowEvent.

Примеры:
    uv run python manage.py import_sheets                    # все active sources
    uv run python manage.py import_sheets --source general   # фильтр по виду
    uv run python manage.py import_sheets --source-id 2 --dry-run
    uv run python manage.py import_sheets --reset            # перематчить всё
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from cargo.models import ImportedSheetRow, SheetSource
from cargo.services.sheets.importer import SheetImporter


class Command(BaseCommand):
    help = 'Импортирует Google Sheets в ImportedSheetRow + (для CRM) события workflow.'

    def add_arguments(self, parser):
        parser.add_argument('--source', choices=['general', 'crm'],
                            help='Фильтр по виду источника (general/crm)')
        parser.add_argument('--source-id', type=int,
                            help='Конкретный ID SheetSource')
        parser.add_argument('--dry-run', action='store_true',
                            help='Читать и считать, но не писать в БД')
        parser.add_argument('--reset', action='store_true',
                            help='Перематчить все строки (сбросить match_status в unmatched)')
        parser.add_argument('--verbose', action='store_true', help='Логировать каждую строку')

    def handle(self, *args, **opts):
        qs = SheetSource.objects.filter(is_active=True)
        if opts['source']:
            qs = qs.filter(kind=opts['source'])
        if opts['source_id']:
            qs = qs.filter(pk=opts['source_id'])

        sources = list(qs)
        if not sources:
            raise CommandError('Активных источников по фильтру нет.')

        if opts['reset']:
            updated = ImportedSheetRow.objects.filter(
                source__in=sources
            ).update(match_status='unmatched', diff_summary={})
            self.stdout.write(self.style.WARNING(f'Сброшено match_status у {updated} строк.'))

        for src in sources:
            self.stdout.write(f'\n→ Импорт {src} (dry_run={opts["dry_run"]})')
            importer = SheetImporter(
                src,
                dry_run=opts['dry_run'],
                verbose=opts['verbose'],
            )
            run = importer.run_once()
            self.stdout.write(self._format_run(run))

    def _format_run(self, run) -> str:
        if run.status == 'error':
            return self.style.ERROR(
                f'  ERROR: {(run.error_message or "")[:300]}'
            )
        return self.style.SUCCESS(
            f'  OK: total={run.rows_total} new={run.rows_new} '
            f'changed={run.rows_changed} unchanged={run.rows_unchanged} '
            f'matched={run.rows_matched} orphan={run.rows_orphan} '
            f'conflict={run.rows_conflict}'
        )
