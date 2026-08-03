"""migrate, создающий ТОЛЬКО схему — RunPython data-миграции пропускаются.

Нужно для настройки пустой PostgreSQL перед батч-копией (migrate_to_pg):
- Схемные операции (CreateModel/AddField/AddIndex/...) применяются.
- RunPython-бэкфиллы (seed DocumentType/шаблонов/норм, backfill cargo_type,
  envelope_id и т.п.) ПРОПУСКАЮТСЯ — их результат уже лежит в SQLite и
  приедет копией. На пустой БД они всё равно либо no-op, либо (латентный баг
  0017: обращение к конкретной модели с будущими колонками) падают.
- Все миграции отмечаются applied (django_migrations заполняется корректно).

НЕ трогает файлы миграций — монки-патч RunPython.database_forwards на время
одного вызова migrate, с восстановлением в finally.

    manage.py migrate_schema_only --database pg
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db.migrations.operations.special import RunPython


class Command(BaseCommand):
    help = 'migrate только схемы (RunPython-миграции пропускаются) — для настройки пустой PG.'

    def add_arguments(self, parser):
        parser.add_argument('--database', default='pg', help='Алиас БД-назначения.')
        parser.add_argument('--reset', action='store_true',
                            help='DROP/CREATE public schema перед migrate (чистый лист; '
                                 'через Django-коннект роли-владельца, без psql).')

    def handle(self, *args, **opts):
        db = opts['database']
        if opts['reset']:
            from django.db import connections
            with connections[db].cursor() as c:
                c.execute('DROP SCHEMA IF EXISTS public CASCADE')
                c.execute('CREATE SCHEMA public')
                c.execute('GRANT ALL ON SCHEMA public TO PUBLIC')
            self.stdout.write(self.style.WARNING(
                f'reset: public schema dropped+recreated on {db}'))
        orig_fwd = RunPython.database_forwards

        def _skip(self, app_label, schema_editor, from_state, to_state):
            return None

        RunPython.database_forwards = _skip
        try:
            self.stdout.write(f'migrate (schema-only, RunPython skipped) -> {db}')
            call_command('migrate', database=db, interactive=False, verbosity=1)
        finally:
            RunPython.database_forwards = orig_fwd
        self.stdout.write(self.style.SUCCESS('schema-only migrate done'))
