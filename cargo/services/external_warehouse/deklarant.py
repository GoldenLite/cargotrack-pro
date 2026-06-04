"""Клиент к API «Декларант Плюс» — склад-API «Мой груз.ВХ».

Семантика:
- Источник СВХ-данных для Дальнего Востока (склад «Таможенный портал»,
  WHInn=2536209470). Альта-СВХ обслуживает только Внуково — конфликта нет.
- moscow-cargo покрывает префиксы 784/555/826/537/880 (Шереметьево) — тоже
  отдельный поток.
- ВАЖНО: даже если QR-сессия видит любые склады ДВ, мы применяем данные
  ТОЛЬКО для нашего склада. WHInn-фильтр встроен в `fetch()` — чужие
  склады возвращают None независимо от того что вернул API.

Авторизация:
- QR-сессия (3 поля: login, sessionId, isMobileUser) хранится в БД
  (DeklarantSession). На VPS QR не отсканить — сессия заводится командой
  `deklarant_login` на машине с планшетом, потом переносится.
- При 401 → `DeklarantAuthError` → caller mark_dead + алерт. Без молчаливой
  деградации.

API:
- Base: https://mdt.deklarant.ru
- Адрес склад-API получаем через GET /api/Settings/GetWhApiAddress
  (≈ https://mdt.deklarant.ru:48774). Не хардкодим.
- Поиск: POST /api/WH/GetInfoByDocumentNumber?region=107
  body = {"pattern": "<AWB/коносамент>", "Stype": "ExactMatch"}
- Ответ: list[≤49] объектов с DOInfo/WHInfo/Documents.
"""
from __future__ import annotations

import json
import logging
from typing import Optional
from urllib.parse import urlparse

import requests
from django.conf import settings

from cargo.models import DeklarantSession


logger = logging.getLogger('cargo.external.deklarant')


# Идентификация склада «Таможенный портал» (Владивосток).
# Прибито на уровне клиента — данные с любого ДРУГОГО склада клиент
# возвращает как None (фильтр перед нормализацией).
TARGET_WH_INN = '2536209470'

REGION = '107'  # ДВТУ (Дальний Восток)
TIMEOUT = 30
USER_AGENT = 'CargoTrack/1.0 (+https://cargo-track.pro)'


class DeklarantError(Exception):
    """Базовая ошибка клиента."""


class DeklarantAuthError(DeklarantError):
    """Сессия мертва (401 / прогнили заголовки). Caller должен mark_dead + алерт."""


class DeklarantClient:
    """HTTP-клиент с переиспользованием QR-сессии из БД.

    Использовать как контекст-менеджер или явно вызывать `close()`:

        with DeklarantClient.from_db() as client:
            if client and client.session_ok():
                info = client.fetch('YILI-004')
    """

    def __init__(self, session: DeklarantSession) -> None:
        self._db_session = session
        self._http = requests.Session()
        self._http.headers.update({
            'User-Agent': USER_AGENT,
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'login': session.login or '',
            'sessionId': session.session_id,
            'isMobileUser': 'true' if session.is_mobile else 'false',
        })
        # SSL verify. mdt.deklarant.ru:48774 (порт склад-API) использует серт
        # которого нет в certifi-bundle requests. Probe-скрипт работает через
        # urllib (системный truststore), здесь явно вырубаем при False.
        self._verify_ssl = bool(getattr(settings, 'DEKLARANT_SSL_VERIFY', False))
        if not self._verify_ssl:
            # Глушим SSLVerifyWarning от urllib3 чтобы лог не засрался
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass
        self._wh_base: Optional[str] = None
        self._mdt_base = (getattr(settings, 'DEKLARANT_MDT_BASE', '') or
                          'https://mdt.deklarant.ru').rstrip('/')
        self._region = getattr(settings, 'DEKLARANT_REGION', None) or REGION
        self._target_wh_inn = (getattr(settings, 'DEKLARANT_TARGET_WH_INN', '')
                               or TARGET_WH_INN)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self._http.close()

    # ── Конструкторы ──

    @classmethod
    def from_db(cls) -> Optional['DeklarantClient']:
        """Берёт активную сессию из БД. None если нет — caller сам решит что делать."""
        s = DeklarantSession.get_active()
        if not s:
            logger.warning('deklarant: no active session in DB')
            return None
        return cls(s)

    # ── Health ──

    def session_ok(self) -> bool:
        """Дешёвый health-check. True если сессия принимается.

        Возможен полу-живой случай: портал жив, доступ к WH отозван. Этот
        метод его не ловит — для полной проверки делаем дешёвый WH-вызов
        (`GetWhApiAddress`).
        """
        try:
            r = self._http.get(f'{self._mdt_base}/api/Account/IsLimited',
                               timeout=TIMEOUT, verify=self._verify_ssl)
            if r.status_code == 200:
                # Параллельно убеждаемся что WH-доступ есть
                addr = self._get_wh_address()
                return bool(addr)
            return False
        except requests.RequestException as e:
            logger.warning('deklarant: session_ok request failed: %s', e)
            return False

    # ── Получение адреса склад-API ──

    def _get_wh_address(self) -> str:
        """GET /api/Settings/GetWhApiAddress → база склад-API.

        Кешируется в инстансе. Адрес НЕ хардкодим — портал отдаёт его
        динамически (есть варианты с токеном в query и без).
        """
        if self._wh_base:
            return self._wh_base
        try:
            r = self._http.get(f'{self._mdt_base}/api/Settings/GetWhApiAddress',
                               timeout=TIMEOUT, verify=self._verify_ssl)
        except requests.RequestException as e:
            raise DeklarantError(f'GetWhApiAddress request failed: {e}')
        if r.status_code == 401:
            raise DeklarantAuthError('GetWhApiAddress returned 401')
        r.raise_for_status()

        try:
            j = r.json()
        except ValueError:
            j = None

        raw: Optional[str] = None
        if isinstance(j, str):
            raw = j
        elif isinstance(j, dict):
            for k in ('Content', 'Address', 'address', 'url', 'Url'):
                if isinstance(j.get(k), str):
                    raw = j[k]
                    break
        if raw is None:
            raw = r.text.strip().strip('"')

        if not raw:
            return ''
        if '?' in raw:
            u = urlparse(raw)
            raw = f'{u.scheme}://{u.netloc}{u.path}'
        self._wh_base = raw.rstrip('/')
        return self._wh_base

    # ── Основной публичный API ──

    def fetch(self, awb_number: str) -> Optional[dict]:
        """Поиск партии в склад-API + фильтр по нашему складу.

        Возвращает нормализованный dict совместимый с
        `external_warehouse.applier.apply_to_cargo()`:

        {
          'license':    '10702/130721/10189/6',           # WHCert (лицензия СВХ)
          'reg_number': '10702020/310526/0113381',        # Registration.RegistrationNumber
          'do1_date':   '2026-05-31',                     # DO1.DO1date (только дата)
          'do1_number_internal': '0000156',               # внутренний № ДО1
          'wh_name': 'ООО "ТАМОЖЕННЫЙ ПОРТАЛ"',           # для аудита
          'wh_inn':  '2536209470',                        # для аудита
          'do2_count': 4,                                 # сколько ДО2 (не пишем, для логирования)
        }

        None если:
        - сетевая ошибка / не-200
        - пустой ответ ([])
        - все элементы ответа — НЕ наш склад (WHInn != TARGET_WH_INN)
        - наш склад нашёлся но DOReject не пустой (отказ в регистрации ДО1 —
          трактуем как «не размещён», не пишем неверные данные)

        Кидает DeklarantAuthError если сессия 401.
        """
        wh_base = self._get_wh_address()
        if not wh_base:
            logger.warning('deklarant: wh_base пуст (нет складского доступа)')
            return None

        url = f'{wh_base}/api/WH/GetInfoByDocumentNumber?region={self._region}'
        body = {'pattern': awb_number, 'Stype': 'ExactMatch'}
        try:
            r = self._http.post(url, json=body, timeout=TIMEOUT,
                                verify=self._verify_ssl)
        except requests.RequestException as e:
            logger.warning('deklarant: fetch %s: request failed: %s', awb_number, e)
            return None

        if r.status_code == 401:
            raise DeklarantAuthError('fetch returned 401')
        if r.status_code >= 500:
            logger.warning('deklarant: fetch %s: HTTP %s', awb_number, r.status_code)
            return None
        if not (200 <= r.status_code < 300):
            logger.info('deklarant: fetch %s: HTTP %s', awb_number, r.status_code)
            return None

        try:
            items = r.json()
        except ValueError:
            logger.warning('deklarant: fetch %s: bad JSON', awb_number)
            return None

        if not isinstance(items, list) or not items:
            return None  # пусто или не наш склад в этом регионе

        # Ищем элемент с нашим WHInn. Игнорируем DOReject (отказы) и пустой ДО1.
        for item in items:
            wh_info = (item or {}).get('WHInfo') or {}
            wh_inn_resp = (wh_info.get('WHInn') or '').strip()
            if wh_inn_resp != self._target_wh_inn:
                continue
            do_info = (item or {}).get('DOInfo') or {}
            do1 = do_info.get('DO1') or {}
            if do_info.get('DOReject') is not None:
                logger.info('deklarant: %s найден на нашем складе, но DOReject != null', awb_number)
                continue
            registration = do1.get('Registration') or {}
            reg_number = (registration.get('RegistrationNumber') or '').strip()
            license_ = (wh_info.get('WHCert') or do1.get('CertNumber') or '').strip()
            do1_date_raw = (do1.get('DO1date') or '').strip()
            # YYYY-MM-DDTHH:MM:SS → YYYY-MM-DD
            do1_date = do1_date_raw[:10] if do1_date_raw else ''
            if not (license_ or reg_number or do1_date):
                continue  # ДО1 ещё не подан — поле пустое

            self._db_session.touch()  # успешный запрос — обновляем last_used_at
            return {
                'license':              license_,
                'reg_number':           reg_number,
                'do1_date':             do1_date,
                'do1_number_internal':  (do1.get('DO1number') or '').strip(),
                'wh_name':              wh_info.get('WHName') or '',
                'wh_inn':               wh_inn_resp,
                'do2_count':            len(do_info.get('DO2') or []),
            }

        # Ничего не нашли (наш склад отсутствует в ответе)
        return None
