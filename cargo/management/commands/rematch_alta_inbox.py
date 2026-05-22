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

from django.core.management.base import BaseCommand

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
        for i, msg in enumerate(qs.iterator(), 1):
            dispatch(msg)
            msg.refresh_from_db(fields=['cargo_id', 'hawb_id', 'status_applied'])
            if msg.cargo_id or msg.hawb_id:
                matched += 1
            if msg.status_applied:
                applied += 1
            if i % 200 == 0:
                self.stdout.write(f'  progress: {i}/{total}  matched={matched}  applied={applied}')

        self.stdout.write(self.style.SUCCESS(
            f'Done. processed={total}, matched={matched}, status_applied={applied}'
        ))
