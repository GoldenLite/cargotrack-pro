"""Помощники для DeklarantSession — load/save/push/pull.

Прямые методы модели (`get_active`, `mark_dead`, `touch`) — на классе
`DeklarantSession` в `cargo/models.py`. Здесь — функции верхнего уровня
и QR-flow.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Optional, Tuple

import requests
from django.conf import settings

from cargo.models import DeklarantSession


logger = logging.getLogger('cargo.external.deklarant.session')


def _mdt_base() -> str:
    return (getattr(settings, 'DEKLARANT_MDT_BASE', '') or
            'https://mdt.deklarant.ru').rstrip('/')


def save_session(login: str, session_id: str, is_mobile: bool = True) -> DeklarantSession:
    """Делает новую сессию активной, гасит все предыдущие.

    Returns: созданный DeklarantSession.
    """
    # Гасим предыдущие активные
    DeklarantSession.objects.filter(is_active=True).update(is_active=False)
    s = DeklarantSession.objects.create(
        login=login or '',
        session_id=session_id,
        is_mobile=bool(is_mobile),
        is_active=True,
    )
    logger.info('deklarant session saved: login=%s id=%s…', login, session_id[:8])
    return s


def load_active_session() -> Optional[DeklarantSession]:
    """Алиас для DeklarantSession.get_active() для согласованности импорта."""
    return DeklarantSession.get_active()


# ── QR-flow ────────────────────────────────────────────────────────────────

def request_qr_token() -> Tuple[str, requests.Response]:
    """Создаёт GUID токен и регистрирует его у портала.

    Returns (token, response).
    """
    token = str(uuid.uuid4())
    base = _mdt_base()
    r = requests.get(f'{base}/api/Account/AddQRToken?token={token}', timeout=30)
    r.raise_for_status()
    return token, r


def poll_qr_token(token: str,
                  timeout_sec: int = 240,
                  poll_interval: float = 2.5,
                  on_wait=None) -> Optional[dict]:
    """Опрашивает CheckQRToken до получения Session или таймаута.

    `on_wait(comment)` вызывается каждый раз когда статус меняется
    (полезно для CLI-фидбека).

    Returns Content dict { Session, Login, IsMobileUser } или None.
    """
    base = _mdt_base()
    deadline = time.time() + timeout_sec
    last_key = None
    while time.time() < deadline:
        try:
            r = requests.get(f'{base}/api/Account/CheckQRToken?token={token}',
                             timeout=30)
            j = r.json() if r.headers.get('Content-Type', '').startswith('application/json') \
                else json.loads(r.text)
        except Exception:
            time.sleep(poll_interval)
            continue
        status = (j or {}).get('Status') or {}
        if status.get('Success'):
            return (j or {}).get('Content') or {}
        key = status.get('Key')
        if key != last_key and on_wait:
            on_wait(status.get('Comment'))
            last_key = key
        time.sleep(poll_interval)
    return None
