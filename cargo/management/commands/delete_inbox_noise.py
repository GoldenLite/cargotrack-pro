"""Удаляет тех.шум из AltaInboxMessage: CMN.00003/00006/ED.11001/11002.

См. memory feedback_no_tech_noise_inbox. Юзер давно просил.

Batched delete (chunks 500) чтобы не залочить SQLite — пишет auto_sync +
agent параллельно. Между батчами пауза.

Usage:
    manage.py delete_inbox_noise --dry-run          # сухой прогон
    manage.py delete_inbox_noise --days 60          # удалить за 60 дней
    manage.py delete_inbox_noise --days 365         # за год (default = всё)
"""
from __future__ import annotations

import datetime
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from cargo.models import AltaInboxMessage


NOISE_TYPES = ['CMN.00003', 'CMN.00006', 'ED.11001', 'ED.11002']


class Command(BaseCommand):
    help = 'Удаляет тех.шум CMN.00003/00006/ED.11001/11002 из AltaInboxMessage'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=0,
                            help='Удалить только старше N дней (0 = все)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Показать сколько удалится без реального DELETE')
        parser.add_argument('--chunk', type=int, default=500,
                            help='Размер батча (default 500)')
        parser.add_argument('--pause', type=float, default=0.2,
                            help='Пауза между батчами сек (default 0.2)')

    def handle(self, *args, **opts):
        qs = AltaInboxMessage.objects.filter(msg_type__in=NOISE_TYPES)
        if opts['days']:
            cutoff = timezone.now() - datetime.timedelta(days=opts['days'])
            qs = qs.filter(received_at__lt=cutoff)
            self.stdout.write(f'Окно: старше {opts["days"]} дней (< {cutoff:%d.%m.%Y})')
        else:
            self.stdout.write('Окно: все')

        total = qs.count()
        self.stdout.write(f'Найдено: {total} записей типов {NOISE_TYPES}')
        if total == 0 or opts['dry_run']:
            if opts['dry_run']:
                self.stdout.write('DRY RUN — БД не изменена.')
            return

        chunk = opts['chunk']
        pause = opts['pause']
        deleted = 0
        started = time.time()
        while True:
            # Берём ID-чанк и удаляем — короткая транзакция, минимум lock-времени.
            ids = list(qs.values_list('id', flat=True)[:chunk])
            if not ids:
                break
            n_del, _ = AltaInboxMessage.objects.filter(pk__in=ids).delete()
            deleted += n_del
            if deleted % 10000 < chunk:
                rate = deleted / max(1, time.time() - started)
                eta = int((total - deleted) / max(1, rate))
                self.stdout.write(
                    f'  ... {deleted}/{total} удалено '
                    f'({rate:.0f}/сек, ETA {eta}сек)')
            time.sleep(pause)

        elapsed = int(time.time() - started)
        self.stdout.write(self.style.SUCCESS(
            f'Готово: {deleted} удалено за {elapsed} сек.'))
