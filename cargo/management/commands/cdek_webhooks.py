"""Просмотр / удаление подписок на вебхуки СДЭК.

    uv run python manage.py cdek_webhooks                 # список (default)
    uv run python manage.py cdek_webhooks --delete <uuid> # удалить подписку

Полезно при ротации секрета: удалить старую подписку, затем
cdek_register_webhook создаст новую.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Список / удаление подписок на вебхуки СДЭК'

    def add_arguments(self, parser):
        parser.add_argument('--delete', metavar='UUID', default='',
                            help='Удалить подписку по uuid')

    def handle(self, *args, **opts):
        if not getattr(settings, 'CDEK_ENABLED', False):
            raise CommandError('CDEK_ENABLED=false — интеграция выключена')

        from cargo.services.cdek.client import CdekClient, CdekError

        try:
            with CdekClient() as c:
                uuid = (opts['delete'] or '').strip()
                if uuid:
                    res = c.delete_webhook(uuid)
                    self.stdout.write(self.style.SUCCESS(
                        f'Подписка {uuid} удалена: {res}'))
                    return

                webhooks = c.list_webhooks()
                if not webhooks:
                    self.stdout.write('Подписок нет.')
                    return
                self.stdout.write(f'Подписок: {len(webhooks)}')
                for w in webhooks:
                    self.stdout.write(
                        f'  - {w.get("type")} {w.get("url")} (uuid={w.get("uuid")})')
        except CdekError as e:
            raise CommandError(str(e))
