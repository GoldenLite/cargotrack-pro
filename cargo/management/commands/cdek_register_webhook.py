"""Регистрация подписки на вебхуки СДЭК (ORDER_STATUS).

Запускается ОДИН раз (и после смены публичного URL / секрета). Идемпотентно:
если подписка ORDER_STATUS на тот же URL уже есть — ничего не делает.

URL приёмника берётся из CDEK_WEBHOOK_PUBLIC_URL, либо собирается из первого
DJANGO_ALLOWED_HOSTS + CDEK_WEBHOOK_SECRET.

    uv run python manage.py cdek_register_webhook
    uv run python manage.py cdek_register_webhook --dry-run
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def build_webhook_url() -> str:
    """Публичный https-URL приёмника вебхуков."""
    explicit = (getattr(settings, 'CDEK_WEBHOOK_PUBLIC_URL', '') or '').strip()
    if explicit:
        return explicit
    secret = (getattr(settings, 'CDEK_WEBHOOK_SECRET', '') or '').strip()
    if not secret:
        raise CommandError('CDEK_WEBHOOK_SECRET пуст — задайте его в .env')
    hosts = [h for h in (settings.ALLOWED_HOSTS or []) if h not in ('*', '')]
    if not hosts:
        raise CommandError(
            'Не из чего собрать URL: задайте CDEK_WEBHOOK_PUBLIC_URL или '
            'DJANGO_ALLOWED_HOSTS')
    return f'https://{hosts[0]}/api/v1/cdek/webhook/{secret}/'


class Command(BaseCommand):
    help = 'Зарегистрировать подписку на вебхуки СДЭК (ORDER_STATUS)'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Показать URL и существующие подписки, без записи')
        parser.add_argument('--type', default='ORDER_STATUS',
                            help='Тип вебхука (default ORDER_STATUS)')

    def handle(self, *args, **opts):
        if not getattr(settings, 'CDEK_ENABLED', False):
            raise CommandError('CDEK_ENABLED=false — интеграция выключена')

        from cargo.services.cdek.client import CdekClient, CdekError

        url = build_webhook_url()
        type_ = opts['type']
        self.stdout.write(f'Webhook URL: {url}')
        self.stdout.write(f'Type:        {type_}')

        try:
            with CdekClient() as c:
                existing = c.list_webhooks()
                self.stdout.write(f'Существующих подписок: {len(existing)}')
                for w in existing:
                    self.stdout.write(
                        f'  - {w.get("type")} {w.get("url")} (uuid={w.get("uuid")})')

                already = any(
                    (w.get('type') == type_ and (w.get('url') or '').rstrip('/') == url.rstrip('/'))
                    for w in existing
                )
                if already:
                    self.stdout.write(self.style.SUCCESS(
                        'Подписка уже зарегистрирована — пропускаю.'))
                    return

                if opts['dry_run']:
                    self.stdout.write(self.style.WARNING(
                        'dry-run: подписка НЕ создана.'))
                    return

                res = c.register_webhook(url, type_)
                self.stdout.write(self.style.SUCCESS(
                    f'Подписка создана: {res}'))
        except CdekError as e:
            raise CommandError(str(e))
