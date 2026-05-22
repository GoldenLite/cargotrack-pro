"""Передиспатчить уже сохранённые AltaInboxMessage с пустым матчем.

При первом проходе сообщений Cargo/HAWB могло не быть в БД, или outbox
ещё не добежал — match вернул (None, None) и status_applied=False.
После того как мы накатим Cargo (см. ensure_cargos_from_sheets) и
поднимется AltaOutboxObservation, эти inbox можно передиспатчить.

Запуск:
    uv run python manage.py rematch_alta_inbox
    uv run python manage.py rematch_alta_inbox --limit 100
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage
from cargo.services.alta.inbox import dispatch


class Command(BaseCommand):
    help = 'Передиспатчить unmatched AltaInboxMessage (когда появились Cargo/HAWB)'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0,
                            help='Сколько максимум обработать (0 = все)')
        parser.add_argument('--only-unmatched', action='store_true', default=True,
                            help='Только cargo=None AND hawb=None (по умолчанию)')

    def handle(self, *args, **opts):
        qs = AltaInboxMessage.objects.all()
        if opts['only_unmatched']:
            qs = qs.filter(cargo=None, hawb=None)
        if opts['limit']:
            qs = qs[:opts['limit']]

        total = qs.count() if not opts['limit'] else min(opts['limit'], AltaInboxMessage.objects.count())
        self.stdout.write(f'Re-dispatching {total} messages...')

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
            f'Done. processed={total}, newly matched={matched}, status_applied={applied}'
        ))
