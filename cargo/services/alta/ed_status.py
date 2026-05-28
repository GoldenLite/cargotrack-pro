"""Реконструкция ЭД-статуса декларации по истории inbox/outbox сообщений.

Альта-ГТД хранит свой «ЭД-статус» в своей Postgres-БД (доступа нет). Мы
реконструируем близкий аналог по нашим данным:
- AltaInboxMessage (msg_kind: registered/released/rejected/examination/
  hold/withdrawn/customs_request/info, parsed_meta)
- AltaOutboxObservation (msg_type CMN.11335/11349/11024/11023)
- HawbCustomsRequest (запросы документов)
- HawbDeclarationAttempt (переподачи)

Фразы взяты как у Альты (gtdw.exe, Delphi resourcestrings).
"""
from __future__ import annotations

from typing import Optional


# Маппинг msg_kind входящего → основная фраза статуса.
# Приоритет: released/rejected/withdrawn > hold/examination > registered > info.
KIND_TO_STATUS: dict[str, str] = {
    'registered':       'Присвоен номер',
    'examination':      'Идет досмотр (осмотр)',
    'hold':             'Идет проверка',
    'released':         'Выпуск разрешен',
    'rejected':         'Отказано в выпуске',
    'withdrawn':        'Отзыв',
    'customs_request':  'Запрошены док-ты',
    'info':             '',
}

# DecisionCode (для CMN.11350 / CMN.11309) → более точная фраза.
# Перекрывает released/rejected.
DECISION_CODE_TO_STATUS: dict[str, str] = {
    '10': 'Выпуск разрешен',
    '11': 'Выпуск с обеспечением',
    '12': 'Выпуск с обеспечением',
    '13': 'Выпуск с обеспечением',
    '14': 'Выпуск с обеспечением',
    '20': 'Условно выпущена',
    '40': 'Отзыв',
    '50': 'Отзыв',
    '51': 'Отзыв',
    '52': 'Отзыв',
    '53': 'Отзыв',
    '60': 'Выпуск приостановлен',
    '61': 'Выпуск приостановлен',
    '62': 'Выпуск приостановлен',
    '70': 'Продлен',
    '82': 'Считается не поданной',
    '90': 'Отказано в выпуске',
    '91': 'Отказано в выпуске',
    '92': 'Иное решение',
}


def _status_from_msg(msg) -> str:
    """Извлекает наиболее точный статус для одного AltaInboxMessage."""
    pm = msg.parsed_meta or {}
    # 1. DecisionCode/Design самые точные (в release-сообщениях)
    dc = (pm.get('decision_code') or '').strip()
    if dc and dc in DECISION_CODE_TO_STATUS:
        return DECISION_CODE_TO_STATUS[dc]
    dsn = (pm.get('design_code') or '').strip()
    if dsn and dsn in DECISION_CODE_TO_STATUS:
        return DECISION_CODE_TO_STATUS[dsn]
    # 2. msg_kind
    return KIND_TO_STATUS.get(msg.msg_kind, '')


def compute_ed_status(hawb) -> str:
    """Реконструирует ЭД-статус для HAWB по её inbox-сообщениям и outbox.

    Логика:
    1. Если есть финальное событие (released/rejected/withdrawn) — берём ЕГО
       фразу с учётом DecisionCode.
    2. Иначе если был запрос документов (customs_request) или hold/examination —
       соответствующая фраза.
    3. Иначе если есть outbox-подача (CMN.11335/11349/11024) — «Присвоен номер»
       (если уже есть customs_declaration_number) или «Открытие процедуры».
    4. Возвращаем '' если ничего не знаем.

    Накладные флаги:
    - При наличии HawbCustomsRequest добавляем «; Запрошены док-ты!»
      (если основной статус не равен этой фразе уже).
    - При наличии 2+ HawbDeclarationAttempt добавляем «; Корректировка!»
      (переподача = корректировка).
    """
    from cargo.models import AltaInboxMessage, AltaOutboxObservation

    if not hawb or not hawb.pk:
        return ''

    # 1. Финальные/значимые входящие
    msgs = AltaInboxMessage.objects.filter(
        hawb=hawb,
    ).exclude(msg_kind__in=('info', 'svh_placed',
                            'svh_do1_registered', 'svh_do2_registered'))
    latest = msgs.order_by('-prepared_at', '-received_at').first()
    main = _status_from_msg(latest) if latest else ''

    if not main:
        # 2. Нет значимых входящих — смотрим outbox. Подача → "Присвоен номер"
        # если уже есть customs_declaration_number, иначе "Открытие процедуры".
        has_outbox = AltaOutboxObservation.objects.filter(
            hawb=hawb,
            msg_type__in=('CMN.11335', 'CMN.11349', 'CMN.11024', 'CMN.11023'),
        ).exists()
        if has_outbox:
            if (hawb.customs_declaration_number or '').strip():
                main = 'Присвоен номер'
            else:
                main = 'Открытие процедуры'

    # 3. Накладные флаги
    flags: list[str] = []
    try:
        n_req = hawb.customs_requests.count()
        if n_req and 'Запрошены' not in main:
            flags.append('Запрошены док-ты!')
    except Exception:
        pass
    try:
        n_att = hawb.declaration_attempts.count()
        if n_att >= 2 and 'Корректировка' not in main:
            flags.append('Корректировка!')
    except Exception:
        pass

    if not main and not flags:
        return ''
    if main and flags:
        return main + '; ' + '; '.join(flags)
    return main or '; '.join(flags)
