"""Клиент к API СДЭК (CDEK API v2) — только для трекинга статусов доставки.

Боевой контур: https://api.cdek.ru/v2. Авторизация — OAuth 2.0
client_credentials (Account = client_id, Secure password = client_secret).
Токен живёт ~1 час; кешируется в памяти инстанса, refresh — лениво и на 401.

Используем ТОЛЬКО read-методы (заказы read-only) + управление подпиской
на вебхуки. Никаких созданий/правок заказов мы не делаем.

Образец структуры — cargo/services/external_warehouse/moscow_cargo.py
(сессия, TIMEOUT, своя ошибка, refresh-on-4xx-retry-once, контекст-менеджер).

Использование:

    with CdekClient() as c:
        entity = c.get_order_by_im_number('AWB12345')   # → dict | None
        entity = c.get_order_by_uuid('72753031-...')     # → dict | None

Кеш токена — в памяти процесса (без БД/файла): каждый management-run и
каждый запрос вебхука создаёт свой инстанс, TTL ~1ч это покрывает.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests
from django.conf import settings


logger = logging.getLogger('cargo.cdek.client')

TIMEOUT = 15
USER_AGENT = 'CargoTrack/1.0 (+https://cargo-track.pro)'

# Терминальные коды статуса СДЭК — заказ дальше не поедет.
CDEK_TERMINAL_CODES = {'DELIVERED', 'NOT_DELIVERED', 'INVALID'}


class CdekError(Exception):
    """Базовая ошибка клиента СДЭК."""


class CdekConfigError(CdekError):
    """Интеграция не сконфигурирована (выключена или нет creds)."""


class CdekClient:
    """HTTP-клиент СДЭК с OAuth2-токеном в памяти инстанса.

    Контекст-менеджер: одна `requests.Session` переиспользуется на весь
    прогон (как в moscow-cargo), что важно для батч-команд.
    """

    def __init__(self) -> None:
        self.base_url = (getattr(settings, 'CDEK_API_BASE_URL', '')
                         or 'https://api.cdek.ru').rstrip('/')
        self.client_id = getattr(settings, 'CDEK_CLIENT_ID', '') or ''
        self.client_secret = getattr(settings, 'CDEK_CLIENT_SECRET', '') or ''

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': USER_AGENT,
            'Accept': 'application/json',
        })
        self._token: Optional[str] = None
        self._token_deadline: float = 0.0  # time.monotonic() дедлайн

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.session.close()

    def close(self) -> None:
        self.session.close()

    # ── OAuth ──

    def _ensure_creds(self) -> None:
        if not self.client_id or not self.client_secret:
            raise CdekConfigError(
                'CDEK_CLIENT_ID/CDEK_CLIENT_SECRET не заданы — '
                'интеграция СДЭК не сконфигурирована')

    def _get_token(self, force: bool = False) -> str:
        """Возвращает валидный access_token, обновляя при необходимости."""
        if not force and self._token and time.monotonic() < self._token_deadline:
            return self._token

        self._ensure_creds()
        url = f'{self.base_url}/v2/oauth/token'
        r = self.session.post(url, data={
            'grant_type': 'client_credentials',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
        }, timeout=TIMEOUT)
        if r.status_code != 200:
            raise CdekError(f'OAuth token failed: HTTP {r.status_code} {r.text[:200]}')
        data = r.json()
        token = data.get('access_token')
        if not token:
            raise CdekError(f'OAuth token: нет access_token в ответе ({data})')
        # Запас 60с до фактического истечения.
        expires_in = int(data.get('expires_in') or 3600)
        self._token = token
        self._token_deadline = time.monotonic() + max(60, expires_in - 60)
        self.session.headers['Authorization'] = f'Bearer {token}'
        logger.debug('cdek: token refreshed (expires_in=%s)', expires_in)
        return token

    # ── низкоуровневый запрос с refresh-on-401-retry-once ──

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        self._get_token()
        url = f'{self.base_url}{path}'
        kwargs.setdefault('timeout', TIMEOUT)
        r = self.session.request(method, url, **kwargs)
        if r.status_code == 401:
            # Токен протух раньше дедлайна — refresh + retry один раз.
            logger.info('cdek: 401, refreshing token and retrying %s %s', method, path)
            self._get_token(force=True)
            r = self.session.request(method, url, **kwargs)
        return r

    # ── Заказы (read-only) ──

    @staticmethod
    def _entity_from_payload(payload: dict) -> Optional[dict]:
        """Достаёт `entity` из ответа /v2/orders.

        СДЭК отвечает {entity:{...}, requests:[{state, errors}], ...}.
        Если заказ не найден — entity отсутствует/пустой, а в requests
        лежит state=INVALID. Возвращаем None (как moscow-cargo при errorcode).
        """
        if not isinstance(payload, dict):
            return None
        entity = payload.get('entity')
        if not entity or not isinstance(entity, dict):
            return None
        # uuid обязателен для валидной сущности
        if not entity.get('uuid'):
            return None
        return entity

    @staticmethod
    def _is_not_found(payload: dict) -> bool:
        """True если тело ответа сигналит «заказ не найден» (state INVALID).

        СДЭК на ненайденный заказ отдаёт HTTP 400 (не 404!) с
        requests[].state=INVALID и errors[].code вида
        'v2_entity_not_found_*'. Это НЕ ошибка интеграции — заказа просто нет.
        """
        if not isinstance(payload, dict):
            return False
        for req in payload.get('requests') or []:
            if (req.get('state') or '').upper() == 'INVALID':
                for err in req.get('errors') or []:
                    if 'not_found' in (err.get('code') or '').lower():
                        return True
        return False

    def _handle_order_response(self, r: requests.Response, what: str) -> Optional[dict]:
        """Единая обработка ответа /v2/orders: entity | None (не найдено) | raise."""
        if r.status_code == 404:
            return None
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if r.status_code == 200:
            return self._entity_from_payload(payload or {})
        # Не-200: «не найдено» → None, прочее → ошибка.
        if payload is not None and self._is_not_found(payload):
            return None
        raise CdekError(f'{what}: HTTP {r.status_code} {r.text[:200]}')

    def get_order_by_uuid(self, uuid: str) -> Optional[dict]:
        """GET /v2/orders/{uuid} → entity-словарь или None."""
        uuid = (uuid or '').strip()
        if not uuid:
            return None
        r = self._request('GET', f'/v2/orders/{uuid}')
        return self._handle_order_response(r, f'get_order_by_uuid {uuid}')

    def get_order_by_im_number(self, im_number: str) -> Optional[dict]:
        """GET /v2/orders?im_number=<наш hawb_number> → entity-словарь или None."""
        im_number = (im_number or '').strip()
        if not im_number:
            return None
        r = self._request('GET', '/v2/orders', params={'im_number': im_number})
        return self._handle_order_response(r, f'get_order_by_im_number {im_number}')

    # ── Вебхуки (подписка) ──

    def list_webhooks(self) -> list:
        r = self._request('GET', '/v2/webhooks')
        if r.status_code != 200:
            raise CdekError(f'list_webhooks: HTTP {r.status_code} {r.text[:200]}')
        data = r.json()
        # Может вернуться list или {entity:[...]} в зависимости от версии.
        if isinstance(data, list):
            return data
        return data.get('entity') or data.get('webhooks') or []

    def register_webhook(self, url: str, type_: str = 'ORDER_STATUS') -> dict:
        r = self._request('POST', '/v2/webhooks', json={'url': url, 'type': type_})
        if r.status_code not in (200, 201, 202):
            raise CdekError(f'register_webhook: HTTP {r.status_code} {r.text[:300]}')
        return r.json()

    def delete_webhook(self, uuid: str) -> dict:
        uuid = (uuid or '').strip()
        if not uuid:
            raise CdekError('delete_webhook: пустой uuid')
        r = self._request('DELETE', f'/v2/webhooks/{uuid}')
        if r.status_code not in (200, 202, 204):
            raise CdekError(f'delete_webhook {uuid}: HTTP {r.status_code} {r.text[:200]}')
        return r.json() if r.text else {}


def extract_statuses(entity: dict) -> dict:
    """Нормализует entity заказа в плоскую структуру для applier.

    Возврат:
    {
      'uuid':        '72753031-...',
      'cdek_number': '1106321645',
      'number':      'AWB12345',          # = наш im_number = hawb_number
      'statuses':    [{'code','name','date_time','city'}, ...],  # как пришло
      'current':     {'code','name','date_time','city'} | None,  # самый свежий
    }
    """
    entity = entity or {}
    raw_statuses = entity.get('statuses') or []
    statuses = []
    for s in raw_statuses:
        if not isinstance(s, dict):
            continue
        statuses.append({
            'code': (s.get('code') or '').strip(),
            'name': (s.get('name') or '').strip(),
            'date_time': (s.get('date_time') or '').strip(),
            'city': (s.get('city') or '').strip(),
        })

    # «Текущий» = самый свежий по date_time. СДЭК обычно отдаёт уже по
    # возрастанию времени, но не полагаемся на порядок — берём max.
    current = None
    for s in statuses:
        if not s['date_time']:
            continue
        if current is None or s['date_time'] >= current['date_time']:
            current = s
    if current is None and statuses:
        current = statuses[-1]

    return {
        'uuid': (entity.get('uuid') or '').strip(),
        'cdek_number': str(entity.get('cdek_number') or '').strip(),
        'number': (entity.get('number') or '').strip(),
        'statuses': statuses,
        'current': current,
    }
