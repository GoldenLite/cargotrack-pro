"""Передиспатчить AltaInboxMessage.

По умолчанию — только unmatched (cargo=None AND hawb=None): полезно когда
после ensure_cargos_from_sheets / outbox добежал и появилось чем матчить.

С `--all` — переобрабатывает ВСЁ, включая уже привязанные. Нужно когда
поменялась classify-логика (например добавили парсинг Design в kind),
чтобы старые сообщения получили актуальный msg_kind и пересчитали ДТ
через recompute_declaration.

Запуск:
    uv run python manage.py rematch_alta_inbox
    uv run python manage.py rematch_alta_inbox --all
    uv run python manage.py rematch_alta_inbox --limit 100
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db import connection, OperationalError

from cargo.models import AltaInboxMessage
from cargo.services.alta.inbox import dispatch


class Command(BaseCommand):
    help = 'Передиспатчить AltaInboxMessage (unmatched по умолчанию, --all для всех)'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0,
                            help='Сколько максимум обработать (0 = все)')
        parser.add_argument('--all', action='store_true', default=False,
                            help='Перебрать ВСЕ сообщения (для переклассификации)')

    def handle(self, *args, **opts):
        # Поднять busy_timeout: waitress пишет в ту же БД, иначе быстро падаем
        # на 'database is locked' при первом конфликте.
        if connection.vendor == 'sqlite':
            with connection.cursor() as c:
                c.execute('PRAGMA busy_timeout=60000;')

        qs = AltaInboxMessage.objects.all().order_by('prepared_at', 'received_at')
        if not opts['all']:
            qs = qs.filter(cargo=None, hawb=None)
        if opts['limit']:
            qs = qs[:opts['limit']]

        total = qs.count() if not opts['limit'] else min(opts['limit'], AltaInboxMessage.objects.count())
        scope = 'ALL' if opts['all'] else 'unmatched only'
        self.stdout.write(f'Re-dispatching {total} messages ({scope})...')

        matched = 0
        applied = 0
        errors = 0
        # Перевести в список — iterator() держит курсор открытым и сам блокирует
        msgs = list(qs.values_list('pk', flat=True))
        for i, pk in enumerate(msgs, 1):
            # Каждое сообщение — отдельная транзакция; при OperationalError
            # ретраим до 3 раз с backoff.
            for attempt in range(3):
                try:
                    msg = AltaInboxMessage.objects.get(pk=pk)
                    dispatch(msg)
                    msg.refresh_from_db(fields=['cargo_id', 'hawb_id', 'status_applied'])
                    if msg.cargo_id or msg.hawb_id:
                        matched += 1
                    if msg.status_applied:
                        applied += 1
                    break
                except OperationalError as e:
                    if 'locked' in str(e).lower() and attempt < 2:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    errors += 1
                    if errors < 10:
                        self.stdout.write(f'  ERR msg {pk}: {e}')
                    break
                except Exception as e:
                    errors += 1
                    if errors < 10:
                        self.stdout.write(f'  ERR msg {pk}: {e}')
                    break
            if i % 200 == 0:
                self.stdout.write(
                    f'  progress: {i}/{total}  matched={matched}  applied={applied}  errors={errors}')

        self.stdout.write(self.style.SUCCESS(
            f'Done. processed={len(msgs)}, matched={matched}, applied={applied}, errors={errors}'
        ))
