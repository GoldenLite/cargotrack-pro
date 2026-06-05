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
    'registered':            'Присвоен номер',
    'examination':           'Идет досмотр (осмотр)',
    'hold':                  'Идет проверка',
    'released':              'Выпуск разрешен',
    'rejected':              'Отказано в выпуске',
    'withdrawn':             'Отзыв',
    'customs_request':       'Запрошены док-ты',
    'registration_rejected': 'Считается не поданной',  # CMN.11062 — отказ в
                                                       # регистрации ДТ
                                                       # (терминальное состояние
                                                       # подачи, рег.номер не
                                                       # присвоен)
    'info':                  '',
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


def _status_from_msg(msg, hawb_number: str = '') -> str:
    """Извлекает наиболее точный статус для одного AltaInboxMessage.

    Multi-consignment защита: в CMN.11350 для multi-HAWB ДТ таможня
    может выдать РАЗНЫЕ решения для разных накладных одной ДТ. Структура:
        parsed_meta['decision_code'] = '10'   # top-level = первое решение
        parsed_meta['consignments'] = [
            {'decision_code': '10', 'waybills': ['10268642359']},
            {'decision_code': '70', 'waybills': ['10271504146']},
        ]
    Если знаем hawb_number — сначала ищем per-consignment решение, чтобы
    не пробросить top-level '10' (выпуск) на HAWB с решением '70' (продление).

    Памятка из репо: feedback `multi_waybill_per_msg`.
    """
    pm = msg.parsed_meta or {}
    # 0. Multi-consignment: per-HAWB решение (защита от cross-pollination)
    if hawb_number:
        for cons in (pm.get('consignments') or []):
            wbs = cons.get('waybills') or []
            if hawb_number in wbs:
                dc_c = (cons.get('decision_code') or '').strip()
                if dc_c and dc_c in DECISION_CODE_TO_STATUS:
                    return DECISION_CODE_TO_STATUS[dc_c]
                # Match по waybills есть, но decision_code пустой/неизвестен
                # → fallthrough к top-level (как раньше).
                break
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

    # 1. ФИНАЛЬНЫЕ источники: release_date / attempt RELEASED/REJECTED.
    # ВАЖНО: финал применяется только если он относится к ТЕКУЩЕЙ ДТ.
    # Сценарий переподачи: старая ДТ была released, потом юзер подал
    # новую — release_date от старой не очищается. Если current_decl
    # отличается от ДТ при которой был release, нельзя показывать
    # «Выпуск разрешен» — current ДТ в работе.
    main = ''
    cur_decl = (hawb.customs_declaration_number or '').strip()
    if cur_decl:
        cur_attempt = hawb.declaration_attempts.filter(
            declaration_number=cur_decl).first()
        if cur_attempt:
            if cur_attempt.status == 'RELEASED':
                main = 'Выпуск разрешен'
            elif cur_attempt.status == 'REJECTED':
                main = 'Отказано в выпуске'
            # FILED для current — релиз был от ДРУГОЙ (старой) ДТ,
            # не используем release_date. Падаем дальше в latest CMN.
    elif hawb.release_date:
        # Нет current decl, но есть release_date — легаси-кейс,
        # сохраняем старое поведение.
        main = 'Выпуск разрешен'

    # 1.5. Если финальных нет — смотрим последнее значимое CMN-сообщение.
    # Расширяем поиск: либо FK hawb=hawb, либо msg.raw_xml упоминает
    # hawb_number И msg.cargo=hawb.mawb. Это нужно для multi-HAWB ДТ:
    # CMN.11350 может быть привязан только к одной HAWB (matched), но
    # упоминает всех siblings в raw_xml.
    from django.db.models import Q
    cond = Q(hawb=hawb)
    if hawb.mawb_id and hawb.hawb_number:
        cond = cond | (Q(raw_xml__icontains=hawb.hawb_number)
                       & Q(cargo=hawb.mawb))
    msgs = AltaInboxMessage.objects.filter(
        cond,
    ).exclude(msg_kind__in=('info', 'svh_placed',
                            'svh_do1_registered', 'svh_do2_registered'))
    latest = msgs.order_by('-prepared_at', '-received_at').first()
    if not main:
        main = _status_from_msg(latest, hawb.hawb_number) if latest else ''

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
        # Outbox может быть FK-привязан к HAWB ИЛИ упомянут только через
        # parsed_meta.hawbs (multi-HAWB ДТ). Проверяем оба пути.
        outbox_qs = AltaOutboxObservation.objects.filter(
            msg_type__in=('CMN.11335', 'CMN.11349', 'CMN.11024', 'CMN.11023'),
        )
        has_outbox = outbox_qs.filter(hawb=hawb).exists()
        if not has_outbox:
            # Кеш: hawb_number → True. Считаем один раз на процесс.
            # Без кеша для каждой HAWB без FK мы итерировали все ~10k
            # outbox-наблюдений (~130M операций при batch-audit на 13k HAWB).
            cache = getattr(compute_ed_status, '_outbox_hawb_cache', None)
            if cache is None:
                cache = set()
                for o in outbox_qs.values_list('parsed_meta', flat=True):
                    for hn in ((o or {}).get('hawbs') or []):
                        if hn:
                            cache.add(hn.strip())
                compute_ed_status._outbox_hawb_cache = cache
            if hawb.hawb_number in cache:
                has_outbox = True
        if has_outbox:
            if (hawb.customs_declaration_number or '').strip():
                main = 'Присвоен номер'
            else:
                main = 'Открытие процедуры'

    # 2.5. Sibling-кейс: нет inbox/outbox следа, но есть HawbDeclarationAttempt
    # со status='FILED' для current_decl. Это означает что decl пробросился
    # через _sync_decl_via_outbox с головной HAWB (и _register_attempt
    # создал attempt без filed_date), а собственных событий Альты для этой
    # sibling нет. Декларация фактически присвоена → возвращаем «Присвоен
    # номер». Применяется только если main всё ещё пуст после inbox/outbox
    # проверок (регрессия для других путей исключена).
    if not main and cur_decl:
        has_filed_attempt = hawb.declaration_attempts.filter(
            declaration_number=cur_decl, status='FILED').exists()
        if has_filed_attempt:
            main = 'Присвоен номер'

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


# Negative phrases that imply NOT in customs pipeline (rejected/withdrawn).
# Substring match — также ловит "Отказано в выпуске; Запрошены док-ты!" и т.п.
_T_NEGATIVE_MARKERS = ('Отказ', 'Отзыв', 'Считается не поданной')


def compute_t_value(hawb) -> bool:
    """T checkbox в CRM-вкладке: TRUE когда HAWB подана/в процессе/выпущена,
    FALSE когда отказ, отзыв или не подана.

    Подход: используем compute_ed_status как primary signal (он robust
    к withdrawn-кейсу и empty customs_status + filed_date set). Любая
    непустая фраза БЕЗ negative-маркера = TRUE.
    """
    ed = compute_ed_status(hawb) or ''
    if not ed.strip():
        return False
    if any(m in ed for m in _T_NEGATIVE_MARKERS):
        return False
    return True
