"""Клиент к публичному порталу Шереметьево-Карго (shercargo.ru).

Сайт даёт публичную «Справку по авианакладной» без авторизации. Грузы с
определёнными префиксами AWB (см. `SHERCARGO_PREFIXES` в `applier.py`)
приходят на их СВХ; данные по ДО1 не дублируются в нашу Альта-СВХ.

Endpoint:
    GET /w/pls/pub/www_pub.awb_info?p_lang=R&p_awb_pr=NNN&p_awb_no=NNNNNNNN

Кодировка: cp1251 (windows-1251).

Структура ответа — несколько HTML-таблиц. Нам нужна последняя крупная
таблица «Обработка груза» со столбцами:
    Рейс | Места | Вес | Состояние | ДО1 № | ДО1 дата | Лицензия СВХ |
    «ДО1 зарегистрирован в таможне: Дата» | «ДО1 в таможне: Номер»

Пример (738-06784212):
    VN-063 10.06.26 14:42 | 12 | 109.5 | Размещён 11.06.26 08:27 |
    33119 | 10.06.26 | 10005/230712/10031/7 | 10.06.26 18:55 |
    10005020/100626/0136218

Если ДО1 ещё не зарегистрирован — последние столбцы пусты.
Если AWB вообще не найдена на сайте — таблицы «Обработка груза» нет
или она пустая.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import requests


logger = logging.getLogger('cargo.external.shercargo')

BASE_URL = 'https://www.shercargo.ru/w/pls/pub/www_pub.awb_info'
LICENSE = '10005/230712/10031/7'  # лицензия СВХ Шереметьево-Карго (общая)

_DEFAULT_TIMEOUT = 20


# Дата вида DD.MM.YY или DD.MM.YYYY → YYYY-MM-DD
_DATE_RE = re.compile(r'\b(\d{2})\.(\d{2})\.(\d{2,4})\b')
# Дата+время DD.MM.YY HH:MM
_DATETIME_RE = re.compile(r'\b(\d{2})\.(\d{2})\.(\d{2,4})\s+(\d{1,2}):(\d{2})\b')


def _norm_date(s: str) -> Optional[str]:
    m = _DATE_RE.search(s or '')
    if not m:
        return None
    d, mo, y = m.groups()
    if len(y) == 2:
        y = '20' + y
    return f'{y}-{mo}-{d}'


def _norm_datetime(s: str) -> Optional[str]:
    m = _DATETIME_RE.search(s or '')
    if not m:
        return None
    d, mo, y, hh, mm = m.groups()
    if len(y) == 2:
        y = '20' + y
    return f'{y}-{mo}-{d}T{int(hh):02d}:{mm}:00'


class ShercargoClient:
    """Минимальный клиент. requests.Session под капотом, можно переиспользовать
    между cargo'ами в batch-сценариях (refresh_shercargo cron)."""

    def __init__(self, timeout: int = _DEFAULT_TIMEOUT):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/126.0.0.0 Safari/537.36',
        })

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def fetch(self, awb_number: str) -> Optional[dict]:
        """Возвращает dict с распарсенными СВХ-данными или None если не нашли.

        Формат результата (совместим с applier.apply_to_cargo):
            {
              'license':       '10005/230712/10031/7',
              'reg_number':    '10005020/100626/0136218',
              'do1_datetime':  '2026-06-10T18:55:00',  # момент рег. ДО1 в таможне
              'do1_date':      '2026-06-10',          # дата ДО1
              'do1_number':    '33119',
              'placement_dt':  '2026-06-11T08:27:00', # размещение на СВХ
              'flight':        'VN-063',
            }
        """
        awb = (awb_number or '').strip().upper()
        if len(awb) < 5 or awb[3] != '-':
            return None
        prefix = awb[:3]
        suffix = awb[4:]
        try:
            r = self.session.get(
                BASE_URL,
                params={'p_lang': 'R', 'p_awb_pr': prefix, 'p_awb_no': suffix},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            logger.warning('shercargo fetch failed for %s: %s', awb, e)
            return None
        if r.status_code != 200:
            logger.info('shercargo HTTP %s for %s', r.status_code, awb)
            return None
        try:
            html = r.content.decode('cp1251', errors='replace')
        except Exception:
            html = r.text

        return self._parse_html(html, awb)

    def _parse_html(self, html: str, awb: str) -> Optional[dict]:
        """Ищем строку обработки груза — у неё ровно 9 <td>-ячеек и первая
        начинается с кода рейса (формат `XX-NNN` или `XX-NNNN`).

        Заголовок «Обработка груза» приходит в cp1251 и в зависимости от
        режима strict/replace может не находиться текстовым поиском, поэтому
        опираемся на структурный признак (9 cells + flight pattern).
        """
        def _strip(s: str) -> str:
            return re.sub(r'\s+', ' ',
                          re.sub(r'<[^>]+>', ' ', s or '')).strip()

        target_cells: Optional[list[str]] = None
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S | re.I):
            tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S | re.I)
            if len(tds) != 9:
                continue
            cells_text = [_strip(c) for c in tds]
            first = cells_text[0]
            # Признак нужной строки: рейс XX-NNN... в первой ячейке + лицензия
            # в формате NNNNN/NNNNNN/NNNNN/N в седьмой.
            if (re.match(r'^[A-Za-z]{2}-?\d{2,4}', first)
                    and re.match(r'\d{4,5}/\d{4,6}/\d{4,5}/\d', cells_text[6])):
                target_cells = cells_text
                break

        if target_cells is None:
            logger.info('shercargo: target 9-cell row not found for %s', awb)
            return None
        cells_text = target_cells

        # Маппинг по столбцам (см. HTML структуру в docstring):
        # 0:Рейс  1:Места  2:Вес  3:Состояние  4:ДО1№  5:ДО1дата
        # 6:Лицензия  7:ДО1 в таможне дата+время  8:ДО1 в таможне номер
        flight_cell    = cells_text[0] if len(cells_text) > 0 else ''
        state_cell     = cells_text[3] if len(cells_text) > 3 else ''
        do1_number     = cells_text[4] if len(cells_text) > 4 else ''
        do1_date_cell  = cells_text[5] if len(cells_text) > 5 else ''
        license_cell   = cells_text[6] if len(cells_text) > 6 else ''
        do1_reg_dt     = cells_text[7] if len(cells_text) > 7 else ''
        do1_reg_number = cells_text[8] if len(cells_text) > 8 else ''

        out: dict = {}
        if license_cell:
            out['license'] = license_cell
        if do1_reg_number:
            out['reg_number'] = do1_reg_number
        if do1_number:
            out['do1_number'] = do1_number
        dt_iso = _norm_datetime(do1_reg_dt)
        if dt_iso:
            out['do1_datetime'] = dt_iso
        d_iso = _norm_date(do1_date_cell)
        if d_iso:
            out['do1_date'] = d_iso
        # Размещение на СВХ — нам не критично (есть scan_into_bond=do1_date+time),
        # но логируем для отладки.
        place_dt = _norm_datetime(state_cell)
        if place_dt:
            out['placement_dt'] = place_dt
        flight_match = re.search(r'([A-Z]{2}-?\d{2,4})', flight_cell)
        if flight_match:
            out['flight'] = flight_match.group(1)

        if not (out.get('license') or out.get('reg_number') or out.get('do1_date')):
            # Ничего не извлекли — возможно AWB найден но ещё без ДО1.
            logger.info('shercargo: no ДО1 yet for %s', awb)
            return None

        logger.info('shercargo parsed %s: %s', awb, sorted(out.keys()))
        return out
