"""Маппинг колонок Sheets → поля Django + нормализаторы значений."""
from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Optional

from django.utils import timezone


# ── Названия колонок в Sheets ──
# Хранятся как канонические имена. Если в реальной таблице чуть отличается
# (например, регистр или пробелы), допишем синонимы в COLUMN_ALIASES.

GEN_RELEASE_TYPE   = 'Тип груза'           # B2B / B2C / Документы (в Sheets называется «Тип груза»)
GEN_CLIENT_INN     = 'ТО Клиент'           # в текущей таблице нет, оставлено для будущей версии
GEN_PROBLEM        = 'Проблема'            # в текущей таблице нет
GEN_HAWB_NUMBER    = 'Накладная СДЭК'      # ключ матчинга (формат 10205383375)
GEN_TSD            = 'ТСД'                 # формат 784-82334582 (MAWB-подобный)
GEN_ARRIVE_DATE    = 'Дата прибытия'
GEN_BOND_DATE      = 'Дата размещения'
GEN_WAREHOUSE_LIC  = 'Лицензия СВХ (фактическое местонахождение)'
GEN_RESPONSIBLE    = 'Ответственный по ТО'
GEN_VED_MANAGER    = 'Менеджер ВЭД'
GEN_COMMENT        = 'Комментарий'
GEN_DECLARATION    = 'Регистрационный номер ДТ'


# CRM-таблица — колонки этапов workflow. Маппится 1-к-1 в HawbWorkflowEvent.event_type.
# В CRM «Номер накладной» = HAWB (то же что «Накладная СДЭК» в Общее, формат 10208061544).
# «Транспортная накладная» = MAWB-подобный номер (формат 141-53433181).
CRM_HAWB_NUMBER         = 'Номер накладной'
CRM_MAWB_NUMBER         = 'Транспортная накладная'
CRM_WAYBILL_TYPE        = 'Тип накладной'
CRM_ARRIVE_DATE         = 'Дата прибытия в РФ'
CRM_WAREHOUSE           = 'СВХ'
CRM_DECLARANT           = 'ФИО специалиста'
CRM_VED                 = 'ФИО Специалист по ВЭД'
CRM_DECLARATION         = '№ Декларации на выпуск'

CRM_EVENT_MAP = {
    'ТЗ Согласовано':                'TZ_AGREED',
    'Госконтроль':                   'GOV_CONTROL',
    'ВЭД собрал документы':          'VED_DOCS_COLLECTED',
    'Дата предоставления документов':'DOCS_PROVIDED',
    'Досбор документов':             'OTHER',           # флаг, не дата
    'Запрос документов':             'DOCS_REQUESTED',
    'Ответ на запрос':               'DOCS_RESPONSE',
    'Отправлен расчет ТП':           'CALC_SENT',
    'Оплата счета':                  'PAYMENT_DONE',
    'Готово к подаче':               'READY_TO_FILE',
    'Подано на ТО':                  'FILED_FOR_CUSTOMS',
    'Запрос таможни':                'CUSTOMS_REQUEST',
    'Ответ ВЭДа по запросу':         'VED_RESPONSE',
    '№ Декларации на выпуск':        'DECLARATION_ISSUED',
}

# Колонки CRM, не входящие в EVENT_MAP, в которых хранится свободный текст.
# Привязываются как comment к соответствующему «родительскому» событию.
CRM_COMMENT_MAP = {
    'Комментарий к согласованию': 'TZ_AGREED',
    'Комментарий ВЭД':            'VED_DOCS_COLLECTED',
    'Ответ ВЭДа по запросу':      'VED_RESPONSE',
}


# ── Нормализаторы ──

_HAWB_RE = re.compile(r'[\s ]+')


def normalize_hawb_number(raw: str | None) -> str:
    """Убирает пробелы и приводит к верхнему регистру.

    «784 - 82334582» → «784-82334582».
    """
    if not raw:
        return ''
    return _HAWB_RE.sub('', str(raw).strip()).upper()


def normalize_inn(raw: str | None) -> str:
    """Оставляет только цифры; пустое если не похоже на ИНН."""
    if not raw:
        return ''
    digits = re.sub(r'\D', '', str(raw))
    if len(digits) in (10, 12):
        return digits
    return ''


_TRUTHY = {'да', 'yes', '+', '✓', 'v', 'ok', 'есть', 'готово', 'выполнено', 'true', '1'}


def is_truthy_marker(raw: str | None) -> bool:
    """Воспринимать ли значение как «галочку»."""
    if raw is None:
        return False
    s = str(raw).strip().lower()
    return s in _TRUTHY


def parse_date_safe(raw: str | None) -> Optional[datetime]:
    """Пытается распознать дату или дату-время из строки Sheets.

    Принимает форматы:
    - 03.01.2026, 03/01/2026, 2026-01-03
    - 03.01.2026 14:30
    - Excel-сериал-числа не поддерживаем (gspread обычно отдаёт уже строкой).

    Возвращает aware datetime в текущем TZ. Если разобрать не удалось — None.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    # дата + время
    fmts_dt = [
        '%d.%m.%Y %H:%M', '%d.%m.%Y %H:%M:%S',
        '%d/%m/%Y %H:%M', '%d/%m/%Y %H:%M:%S',
        '%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%dT%H:%M:%S',
    ]
    for f in fmts_dt:
        try:
            dt = datetime.strptime(s, f)
            return timezone.make_aware(dt) if timezone.is_naive(dt) else dt
        except ValueError:
            pass

    fmts_d = ['%d.%m.%Y', '%d/%m/%Y', '%Y-%m-%d', '%d.%m.%y']
    for f in fmts_d:
        try:
            d = datetime.strptime(s, f).date()
            return timezone.make_aware(datetime.combine(d, time(0, 0)))
        except ValueError:
            pass

    return None


def map_release_type(raw: str | None) -> str:
    """Колонка «Выпуск» → HouseWaybill.cargo_type."""
    if not raw:
        return ''
    s = str(raw).strip().upper()
    if 'B2B' in s or 'В2В' in s:
        return 'B2B'
    if 'B2C' in s or 'В2С' in s:
        return 'B2C'
    if 'C2C' in s or 'С2С' in s:
        return 'C2C'
    if 'ДОК' in s or 'DOC' in s:
        return 'DOC'
    return ''
