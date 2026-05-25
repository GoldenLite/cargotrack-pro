"""Удаляет технические квитанции из AltaInboxMessage / AltaOutboxObservation.

CMN.00003 (входящие) и CMN.00202 (исходящие) — это технические квитанции
о доставке/прочтении сообщений. Они занимают 11k+ строк каждая в БД,
бизнес-логике не нужны, но участвуют в reparse — замедляют его.

Запуск:
    uv run python manage.py cleanup_alta_quitancii
    uv run python manage.py cleanup_alta_quitancii --dry-run
    uv run python manage.py cleanup_alta_quitancii --types CMN.00003,CMN.00202
"""
from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, AltaOutboxObservation


DEFAULT_TYPES = ('CMN.00003', 'CMN.00202')


class Command(BaseCommand):
    help = 'Удаляет технические квитанции CMN.00003/CMN.00202 из БД'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Только показать сколько удалится')
        parser.add_argument('--types', default=','.join(DEFAULT_TYPES),
                            help='Через запятую — какие msg_type удалять')

    def handle(self, *args, **opts):
        types = [t.strip() for t in opts['types'].split(',') if t.strip()]
        if not types:
            self.stdout.write(self.style.WARNING('--types пуст'))
            return

        for t in types:
            inbox_qs = AltaInboxMessage.objects.filter(msg_type=t)
            outbox_qs = AltaOutboxObservation.objects.filter(msg_type=t)
            n_inbox = inbox_qs.count()
            n_outbox = outbox_qs.count()
            self.stdout.write(
                f'{t}: inbox={n_inbox}, outbox={n_outbox}'
            )
            if opts['dry_run']:
                continue
            if n_inbox:
                inbox_qs.delete()
                self.stdout.write(f'  deleted {n_inbox} from AltaInboxMessage')
            if n_outbox:
                outbox_qs.delete()
                self.stdout.write(f'  deleted {n_outbox} from AltaOutboxObservation')

        if opts['dry_run']:
            self.stdout.write(self.style.NOTICE('dry-run, ничего не удалено'))
        else:
            self.stdout.write(self.style.SUCCESS('Done'))
