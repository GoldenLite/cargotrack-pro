"""Read-only диагностика подключения к СДЭК — БЕЗОПАСНО перед включением.

Ничего не пишет в БД и НЕ регистрирует подписку. Проверяет:
1. OAuth-токен (значит creds верные и контур доступен);
2. существующие подписки на вебхуки (если есть);
3. опционально — конкретный заказ + его историю статусов (чистый GET).

Работает при наличии CDEK_CLIENT_ID/SECRET (CDEK_ENABLED можно не включать).

    uv run python manage.py cdek_check
    uv run python manage.py cdek_check --order <hawb_number>     # = im_number
    uv run python manage.py cdek_check --uuid <order-uuid>
    uv run python manage.py cdek_check --cdek <cdek-number>
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Read-only проверка подключения к СДЭК (токен, подписки, заказ)'

    def add_arguments(self, parser):
        parser.add_argument('--order', default='', metavar='IM_NUMBER',
                            help='im_number (=hawb_number) для тестового GET заказа')
        parser.add_argument('--uuid', default='', help='uuid заказа СДЭК для GET')
        parser.add_argument('--cdek', default='', metavar='CDEK_NUMBER',
                            help='номер заказа СДЭК для GET (через ?cdek_number)')

    def handle(self, *args, **opts):
        cid = getattr(settings, 'CDEK_CLIENT_ID', '') or ''
        if not cid or not (getattr(settings, 'CDEK_CLIENT_SECRET', '') or ''):
            raise CommandError('CDEK_CLIENT_ID/CDEK_CLIENT_SECRET не заданы в .env')

        from cargo.services.cdek.client import CdekClient, CdekError, extract_statuses

        base = getattr(settings, 'CDEK_API_BASE_URL', '')
        self.stdout.write(f'Base URL:  {base}')
        self.stdout.write(f'client_id: {cid[:4]}…{cid[-2:]} (len={len(cid)})')

        try:
            with CdekClient() as c:
                # 1. Токен
                c._get_token()
                self.stdout.write(self.style.SUCCESS('1) OAuth-токен получен ✔'))

                # 2. Подписки
                hooks = c.list_webhooks()
                self.stdout.write(f'2) Подписок на вебхуки: {len(hooks)}')
                for w in hooks:
                    self.stdout.write(
                        f'     - {w.get("type")} {w.get("url")} (uuid={w.get("uuid")})')

                # 3. Опциональный тестовый заказ
                entity = None
                if opts['uuid']:
                    entity = c.get_order_by_uuid(opts['uuid'])
                elif opts['order']:
                    entity = c.get_order_by_im_number(opts['order'])
                elif opts['cdek']:
                    r = c._request('GET', '/v2/orders', params={'cdek_number': opts['cdek']})
                    entity = c._handle_order_response(r, f'get_order_by_cdek {opts["cdek"]}')

                if any((opts['uuid'], opts['order'], opts['cdek'])):
                    if not entity:
                        self.stdout.write(self.style.WARNING(
                            '3) Заказ не найден (проверь, что im_number=hawb_number '
                            'действительно заведён в СДЭК).'))
                    else:
                        ext = extract_statuses(entity)
                        self.stdout.write(self.style.SUCCESS(
                            f'3) Заказ найден: uuid={ext["uuid"]} '
                            f'cdek_number={ext["cdek_number"]} im_number={ext["number"]}'))
                        for s in ext['statuses']:
                            self.stdout.write(
                                f'     {s["date_time"]}  {s["code"]:32s} {s["name"]} '
                                f'{("· " + s["city"]) if s["city"] else ""}')
                        cur = ext['current']
                        if cur:
                            self.stdout.write(f'     → текущий: {cur["code"]} ({cur["name"]})')
        except CdekError as e:
            raise CommandError(f'Ошибка СДЭК: {e}')

        self.stdout.write(self.style.SUCCESS('Готово. БД не менялась, подписка не регистрировалась.'))
