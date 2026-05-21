"""Импорт Google Sheets → ImportedSheetRow + HawbWorkflowEvent.

Примеры:
    uv run python manage.py import_sheets                    # все active sources
    uv run python manage.py import_sheets --source general   # фильтр по виду
    uv run python manage.py import_sheets --source-id 2 --dry-run
    uv run python manage.py import_sheets --rematch          # перематчить уже импортированное (без обращения к Sheets)
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cargo.models import ImportedSheetRow, SheetSource
from cargo.services.sheets.events import emit_workflow_events
from cargo.services.sheets.importer import SheetImporter
from cargo.services.sheets.matcher import match_row


class Command(BaseCommand):
    help = 'Импортирует Google Sheets в ImportedSheetRow + (для CRM) события workflow.'

    def add_arguments(self, parser):
        parser.add_argument('--source', choices=['general', 'crm'],
                            help='Фильтр по виду источника (general/crm)')
        parser.add_argument('--source-id', type=int,
                            help='Конкретный ID SheetSource')
        parser.add_argument('--dry-run', action='store_true',
                            help='Читать и считать, но не писать в БД')
        parser.add_argument('--rematch', action='store_true',
                            help='Не читать Sheets, только заново прогнать matcher по существующим строкам')
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

        if opts['rematch']:
            self._rematch(sources)
            return

        for src in sources:
            self.stdout.write(f'\n→ Импорт {src} (dry_run={opts["dry_run"]})')
            importer = SheetImporter(
                src,
                dry_run=opts['dry_run'],
                verbose=opts['verbose'],
            )
            run = importer.run_once()
            self.stdout.write(self._format_run(run))

    def _rematch(self, sources) -> None:
        for src in sources:
            self.stdout.write(f'\n→ Rematch {src}')
            qs = ImportedSheetRow.objects.filter(source=src).order_by('pk')
            n_total = qs.count()
            n_done = 0
            stats = {'matched': 0, 'orphan': 0, 'conflict': 0, 'ambiguous': 0}
            with transaction.atomic():
                for r in qs.iterator(chunk_size=500):
                    match_row(r)
                    r.save(update_fields=[
                        'hawb_number_raw', 'hawb_number_norm', 'inn_raw',
                        'declaration_number', 'arrival_date',
                        'match_status', 'matched_hawb',
                        'matched_cargo', 'diff_summary',
                    ])
                    if src.kind == 'crm' and r.matched_hawb_id:
                        emit_workflow_events(r)
                    stats[r.match_status] = stats.get(r.match_status, 0) + 1
                    n_done += 1
                    if n_done % 500 == 0:
                        self.stdout.write(f'  {n_done}/{n_total} ...')
            self.stdout.write(self.style.SUCCESS(
                f'  Done: total={n_done} ' + ' '.join(f'{k}={v}' for k, v in stats.items())
            ))

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
