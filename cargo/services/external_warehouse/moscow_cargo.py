"""Клиент к публичному API moscow-cargo.com.

Сайт (Шереметьево/Москва-Карго) — внешний СВХ, не наш. Грузы с
определёнными префиксами AWB (`784`, `555`, `826`, `537`, `880`)
приходят туда. От нашей Альты-СВХ мы по этим партиям ничего не получаем —
работаем чужие склады. Сайт публично отдаёт инфу о размещении (ДО1) через
JSON API.

Endpoint: POST /intapi/statusawb_v8
Body (form-encoded):
    _token=<csrf>
    num=784-84071816
    type=awb
    technology=
    id=
    version=7.7

CSRF: токен из формы поиска на главной странице, `<input name="_token">`.
Живёт N минут (на практике >1 часа), при истечении сервер вернёт 419/403
— тогда обновляем токен и повторяем.

Ответ JSON:
{
  "errorcode": 0,
  "data": {
    "awbinfo": {...},
    "flightinfo": [...],
    "warehouse": [...],
    "status": [...timeline...],
    "do1": [{"do1_number": "0015340", "do1_date": "2026-05-09",
             "license": "10005/181213/10047/9",
             "customs_num": "10005020/090526/0105328", "status": "OK"}],
    "do2": [...]
  }
}

Если по партии ДО1 ещё не подан — `do1` пустой или ключа нет.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import requests


logger = logging.getLogger('cargo.external.moscow_cargo')

BASE_URL = 'https://www.moscow-cargo.com'
ENDPOINT = '/intapi/statusawb_v8'
TIMEOUT = 15
USER_AGENT = 'CargoTrack/1.0 (+https://cargo-track.pro)'

_TOKEN_RE = re.compile(
    r'<input[^>]+name=["\']_token["\'][^>]+value=["\']([^"\']+)["\']'
)


class MoscowCargoError(Exception):
    """Базовая ошибка клиента."""


class MoscowCargoClient:
    """HTTP-клиент с автообновлением CSRF-токена.

    Использовать как контекст-менеджер или явно вызывать `close()`:

        with MoscowCargoClient() as client:
            info = client.fetch('784-84071816')
    """

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': USER_AGENT,
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest',
        })
        self._token: Optional[str] = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.session.close()

    def close(self) -> None:
        self.session.close()

    # ── CSRF ──

    def _refresh_token(self) -> None:
        r = self.session.get(BASE_URL + '/', timeout=TIMEOUT)
        r.raise_for_status()
        m = _TOKEN_RE.search(r.text)
        if not m:
            raise MoscowCargoError('_token не найден на главной странице')
        self._token = m.group(1)
        logger.debug('moscow-cargo: token refreshed (%s...)', self._token[:8])

    # ── API ──

    def fetch_raw(self, awb_number: str) -> dict:
        """POST /intapi/statusawb_v8 с автоматическим refresh токена при 419/403."""
        if not self._token:
            self._refresh_token()

        body = {
            '_token': self._token,
            'num': awb_number,
            'technology': '',
            'id': '',
            'type': 'awb',
            'version': '7.7',
        }

        url = BASE_URL + ENDPOINT
        r = self.session.post(url, data=body, timeout=TIMEOUT)

        # CSRF expired → refresh + retry один раз
        if r.status_code in (419, 403):
            logger.info('moscow-cargo: %s, refreshing token', r.status_code)
            self._refresh_token()
            body['_token'] = self._token
            r = self.session.post(url, data=body, timeout=TIMEOUT)

        r.raise_for_status()
        return r.json()

    def fetch(self, awb_number: str) -> Optional[dict]:
        """Возвращает извлечённые ключевые поля или None если ДО1 ещё нет.

        Структура возврата:
        {
          'do1_number_internal': '0015340',           # внутренний № ДО1 в Москва-Карго
          'do1_date':            '2026-05-09',        # дата подачи ДО1 (= размещения)
          'license':             '10005/181213/10047/9',
          'reg_number':          '10005020/090526/0105328',
          'awb_info':            {...},               # raw — для аудита / Cargo enrichment
          'flight':              {'carrier': 'CZ', 'flight_number': '655', 'flight_date': '2026-05-08'},
        }

        Если partition не найдена / `errorcode != 0` / ДО1 нет → None.
        """
        try:
            raw = self.fetch_raw(awb_number)
        except requests.RequestException as e:
            logger.warning('moscow-cargo: request failed for %s: %s', awb_number, e)
            return None
        except MoscowCargoError as e:
            logger.warning('moscow-cargo: %s', e)
            return None

        if raw.get('errorcode') != 0:
            logger.info('moscow-cargo: errorcode=%s for %s (нет на сайте)',
                        raw.get('errorcode'), awb_number)
            return None

        data = raw.get('data') or {}
        do1_list = data.get('do1') or []
        if not do1_list:
            return None

        # Несколько ДО1 за одну партию = ОЧЕНЬ редко. Берём первый (последний
        # действующий) — обычно один.
        do1 = do1_list[0]
        flight_list = data.get('flightinfo') or []
        flight = flight_list[0] if flight_list else {}

        # ── Сверка «заявлено по авианакладной» vs «фактически принято складом» ──
        # Груз прилетает НЕ ЦЕЛИКОМ регулярно (часть мест остаётся в аэропорту
        # вылета / летит следующим рейсом). Тогда ДО1 оформляется только на
        # прилетевшую часть, и разливать его на ВСЕ накладные партии нельзя —
        # неизвестно, какие именно места приехали (кейс 784-84705375, 20.07.2026:
        # заявлено 43 места / 238 кг, принято 26 / 90, ДО1 на 26).
        awbinfo = data.get('awbinfo') or {}
        wh_list = data.get('warehouse') or []
        wh = wh_list[0] if wh_list else {}

        declared_pieces = _to_int(awbinfo.get('pieces'))
        declared_weight = _to_float(awbinfo.get('weight'))
        # Фактическое: приоритет orig_* из самого ДО1, иначе блок warehouse.
        arrived_pieces = _to_int(do1.get('orig_pieces'))
        if arrived_pieces is None:
            arrived_pieces = _to_int(wh.get('pieces'))
        arrived_weight = _to_float(do1.get('orig_weight'))
        if arrived_weight is None:
            arrived_weight = _to_float(wh.get('weight'))

        is_partial = (declared_pieces is not None
                      and arrived_pieces is not None
                      and arrived_pieces < declared_pieces)
        if is_partial:
            logger.warning(
                'moscow-cargo: ЧАСТИЧНОЕ прибытие %s — заявлено %s мест/%s кг, '
                'принято %s мест/%s кг. ДО1 к партии НЕ применяем.',
                awb_number, declared_pieces, declared_weight,
                arrived_pieces, arrived_weight)

        return {
            'do1_number_internal': (do1.get('do1_number') or '').strip(),
            'do1_date':            (do1.get('do1_date') or '').strip(),
            'license':             (do1.get('license') or '').strip(),
            'reg_number':          (do1.get('customs_num') or '').strip(),
            'awb_info':            awbinfo,
            'flight':              flight,
            # Сверка мест/веса
            'declared_pieces':     declared_pieces,
            'declared_weight':     declared_weight,
            'arrived_pieces':      arrived_pieces,
            'arrived_weight':      arrived_weight,
            'is_partial':          is_partial,
        }


def _to_int(v) -> Optional[int]:
    """'26' / 26 / '' / None → int | None (без падений на мусоре)."""
    if v is None or v == '':
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _to_float(v) -> Optional[float]:
    """'90.000' / 90 / '' / None → float | None."""
    if v is None or v == '':
        return None
    try:
        return float(str(v).strip().replace(',', '.'))
    except (TypeError, ValueError):
        return None
