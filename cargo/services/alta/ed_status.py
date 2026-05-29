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

# Цветовая палитра как в Альте — RGB (0..1) для gspread format.
# Светлые тона чтобы не давить на глаза в больших таблицах.
_RED   = {'red': 1.00, 'green': 0.80, 'blue': 0.82}   # тревога/запрос
_GREEN = {'red': 0.78, 'green': 0.92, 'blue': 0.78}   # выпуск
_YELLOW= {'red': 1.00, 'green': 0.95, 'blue': 0.62}   # промежуточный
_BEIGE = {'red': 0.96, 'green': 0.90, 'blue': 0.74}   # начальный/корректировка
_OLIVE = {'red': 0.85, 'green': 0.85, 'blue': 0.60}   # отказ
_GREY  = {'red': 0.88, 'green': 0.88, 'blue': 0.88}   # архив
_WHITE = {'red': 1.0,  'green': 1.0,  'blue': 1.0}    # default

# Маппинг ключевой фразы → цвет фона. Совпадение по substring чтобы
# обрабатывать комбинации с флагами ("Продлен; Запрошены док-ты!").
STATUS_BG_COLOR: list[tuple[str, dict]] = [
    # Тревожные (краснее)
    ('Запрошены док-ты',       _RED),
    ('Идет проверка',          _RED),
    ('Идет досмотр',           _RED),
    ('Корректировка',          _RED),
    ('Оплатить сбор',          _RED),
    ('Требуется оплата',       _RED),
    ('Уведомление о досмотре', _RED),
    ('Сканирование оригиналов',_RED),
    ('Подтвердите прибытие',   _RED),
    ('Получен протокол ошибок',_RED),
    ('Ошибка',                 _RED),
    # Выпущено (зелёные)
    ('Выпуск разрешен',        _GREEN),
    ('Выпуск с обеспечением',  _GREEN),
    ('Условно выпущена',       _GREEN),
    ('Решение различно',       _GREEN),
    ('Иное решение',           _GREEN),
    ('Получен чек оплаты',     _GREEN),
    ('Квитанция оплачена',     _GREEN),
    # Отказ / отзыв (оливковый)
    ('Отказано в выпуске',     _OLIVE),
    ('Отказано в приеме',      _OLIVE),
    ('Считается не поданной',  _OLIVE),
    ('Отзыв',                  _OLIVE),
    ('Выпуск приостановлен',   _OLIVE),
    # Промежуточные (жёлтый)
    ('Продлен',                _YELLOW),
    ('Присвоен номер',         _YELLOW),
    ('Проверка стоимости',     _YELLOW),
    ('Проверка ТНВЭД',         _YELLOW),
    ('Проверка страны',        _YELLOW),
    ('Проверка закончена',     _YELLOW),
    # Начальные (бежевый)
    ('Открытие процедуры',     _BEIGE),
    ('Товар прибыл',           _BEIGE),
    # Архив (серый)
    ('Отправлен в архив',      _GREY),
    ('Старый архив',           _GREY),
    ('В архиве',               _GREY),
    ('Переход на',             _GREY),
]


def bg_color_for_status(status: str) -> dict:
    """RGB-цвет фона для фразы ed_status. _WHITE если фраза неизвестна/пустая."""
    if not status:
        return _WHITE
    for needle, color in STATUS_BG_COLOR:
        if needle in status:
            return color
    return _WHITE


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

    # 1. ФИНАЛЬНЫЕ источники (release_date / attempt RELEASED/REJECTED) —
    # высший приоритет. Финальный факт выпуска/отказа должен пересиливать
    # промежуточные фразы вроде 'Присвоен номер' от CMN.11337 (которое
    # таможня могла прислать ПОСЛЕ выпуска как уведомление).
    main = ''
    cur_decl = (hawb.customs_declaration_number or '').strip()
    if hawb.release_date:
        main = 'Выпуск разрешен'
    elif cur_decl:
        cur_attempt = hawb.declaration_attempts.filter(
            declaration_number=cur_decl).first()
        if cur_attempt:
            if cur_attempt.status == 'RELEASED':
                main = 'Выпуск разрешен'
            elif cur_attempt.status == 'REJECTED':
                main = 'Отказано в выпуске'

    # 1.5. Если финальных нет — смотрим последнее значимое CMN-сообщение.
    # ВАЖНО: attempt со статусом FILED (без явного RELEASED/REJECTED от
    # таможни) НЕ источник 'Присвоен номер' — backfill_attempts создаёт
    # такой при любой ручной ДТ в Sheets, для легаси-HAWB (выпущенных до
    # подключения агента) это вводило бы в заблуждение. 'Присвоен номер'
    # ставим только когда таможня реально прислала CMN.11337/11001
    # (kind=registered) или другое значимое сообщение.
    msgs = AltaInboxMessage.objects.filter(
        hawb=hawb,
    ).exclude(msg_kind__in=('info', 'svh_placed',
                            'svh_do1_registered', 'svh_do2_registered'))
    latest = msgs.order_by('-prepared_at', '-received_at').first()
    if not main:
        main = _status_from_msg(latest) if latest else ''

    # 1.6. Переподача после финального решения: если у HAWB есть outbox
    # CMN.11023/11349/11335/11024 с prepared_at ПОЗЖЕ финального
    # решения от таможни (rejected/withdrawn) — значит юзер переподал,
    # начинается новая фаза «Открытие процедуры» (или «Присвоен номер»
    # если уже пришёл CMN.11337/11001 на новую ДТ).
    if main in ('Отказано в выпуске', 'Считается не поданной', 'Отзыв'):
        baseline_ts = latest.prepared_at if latest else None
        if baseline_ts:
            # Связь outbox с HAWB: либо FK hawb_id, либо в parsed_meta.hawbs
            # или raw_xml. Полный поиск дорогой; ограничиваем по дате.
            candidates = AltaOutboxObservation.objects.filter(
                msg_type__in=('CMN.11023', 'CMN.11349',
                              'CMN.11335', 'CMN.11024'),
                prepared_at__gt=baseline_ts,
            )
            has_resubmission = False
            for o in candidates:
                if o.hawb_id == hawb.pk:
                    has_resubmission = True
                    break
                pm = o.parsed_meta or {}
                if hawb.hawb_number in (pm.get('hawbs') or []):
                    has_resubmission = True
                    break
            if has_resubmission:
                # Если уже пришёл CMN.11337/11001 на новую подачу с
                # GTDNumber — current_decl уже изменился, и attempt
                # RELEASED/REJECTED не для текущей ДТ. Тогда фраза
                # из latest_msg перебьёт rejected. Здесь главное —
                # вернуть к «открытию процедуры» / «присвоен номер».
                if (hawb.customs_declaration_number or '').strip():
                    main = 'Присвоен номер'
                else:
                    main = 'Открытие процедуры'

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

    # 3. Накладные флаги. При финальных статусах (выпуск / отказ / отзыв /
    # «считается не поданной») запросы документов уже не актуальны и флаг
    # не добавляем — таможня закрыла процедуру.
    FINAL_MAIN = (
        'Выпуск разрешен', 'Выпуск с обеспечением', 'Условно выпущена',
        'Отказано в выпуске', 'Считается не поданной', 'Отзыв',
        'Иное решение', 'Решение различно',
    )
    is_final = any(p in main for p in FINAL_MAIN)
    flags: list[str] = []
    if not is_final:
        try:
            n_req = hawb.customs_requests.count()
            if n_req and 'Запрошены' not in main:
                flags.append('Запрошены док-ты!')
        except Exception:
            pass
    # Флаг «Корректировка!» в Альте = КДТ (изменение декларации после
    # выпуска), а не «переподача с новой декларацией». 2+ attempts у нас
    # = переподача (отказ→новая подача), это не КДТ — не ставим флаг.

    if not main and not flags:
        return ''
    if main and flags:
        return main + '; ' + '; '.join(flags)
    return main or '; '.join(flags)
