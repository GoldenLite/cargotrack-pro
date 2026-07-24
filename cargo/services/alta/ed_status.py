"""Реконструкция ЭД-статуса декларации по истории inbox/outbox сообщений.

Альта-ГТД хранит свой «ЭД-статус» в своей Postgres-БД (доступа нет). Мы
реконструируем близкий аналог по нашим данным:
- AltaInboxMessage (msg_kind: registered/released/rejected/examination/
  hold/withdrawn/customs_request/info, parsed_meta)
- AltaOutboxObservation (msg_type CMN.11335/11349/11024/11023)
- HawbCustomsRequest (запросы документов)
- HawbDeclarationAttempt (переподачи)

Фразы взяты как у Альты (gtdw.exe, Delphi resourcestrings).

Батч-режим (ed_status_batch): плановые команды (audit_sheets_vs_db,
crm_sync_incremental, crm_sort_all, ...) вызывают compute_ed_status на
тысячи HAWB. Шаг 1.5 в одиночном режиме делает per-HAWB SQL с
`raw_xml__icontains` — LIKE по гигантскому TEXT, который с ростом
AltaInboxMessage довёл аудит до убийства по таймлимиту крона (07.07.2026).
Внутри `with ed_status_batch():` «последнее значимое сообщение» строится
один раз НА ПАРТИЮ (один проход по raw_xml сообщений партии в Python)
и кэшируется на время контекста. Вне контекста поведение прежнее —
realtime-путям (waitress) кэш не достаётся, стейл исключён.
"""
from __future__ import annotations

import contextlib
import contextvars
import datetime as _dt
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


# ─────────────────────────── батч-режим ───────────────────────────

# Активный батч-кэш (None вне ed_status_batch). contextvars: не протекает
# в другие треды/реквесты waitress — новый тред видит default (None).
_batch_ctx: contextvars.ContextVar['_EdStatusBatchCache | None'] = \
    contextvars.ContextVar('ed_status_batch', default=None)

# «Минус бесконечность» для сортировки: SQLite в ORDER BY ... DESC ставит
# NULL последним (NULL = наименьшее значение) — повторяем ту же семантику.
_DT_MIN = _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)


class _MsgLite:
    """Лёгкий слепок AltaInboxMessage — ровно то, что нужно _status_from_msg
    (msg_kind, parsed_meta) и шагу 1.6 (prepared_at)."""
    __slots__ = ('pk', 'msg_kind', 'parsed_meta', 'prepared_at', 'received_at')

    def __init__(self, pk, msg_kind, parsed_meta, prepared_at, received_at):
        self.pk = pk
        self.msg_kind = msg_kind
        self.parsed_meta = parsed_meta
        self.prepared_at = prepared_at
        self.received_at = received_at

    def _sort_key(self):
        # pk — детерминированный тай-брейк при равных (prepared_at,
        # received_at); одиночный путь сортирует так же ('-pk').
        return (self.prepared_at or _DT_MIN,
                self.received_at or _DT_MIN, self.pk)


# Сентинел «HAWB не было в снапшоте партии» (создана/перелинкована после
# построения кэша) — вызывающий код падает на одиночный SQL-путь.
_MISSING = object()


# msg_kind, исключаемые из «значимых» (тот же список, что в шаге 1.5).
_INSIGNIFICANT_KINDS = ('info', 'svh_placed',
                        'svh_do1_registered', 'svh_do2_registered')
# Типы outbox-подач (шаги 1.6 и 2).
_SUBMISSION_TYPES = ('CMN.11023', 'CMN.11349', 'CMN.11335', 'CMN.11024')


class _EdStatusBatchCache:
    """Ленивая пред-агрегация «последнее значимое сообщение per-HAWB».

    Строится по требованию НА ПАРТИЮ (cargo): один SQL-запрос за всеми
    значимыми сообщениями партии + один Python-проход по их raw_xml для
    всех номеров HAWB партии — вместо per-HAWB LIKE-запроса. Семантика
    атрибуции 1:1 с одиночным шагом 1.5:
      - FK: msg.hawb_id ∈ HAWB партии → сообщение её (независимо от
        msg.cargo — как Q(hawb=hawb) без условия на cargo);
      - substring: номер HAWB встречается в msg.raw_xml И msg.cargo_id
        == cargo.pk (как Q(raw_xml__icontains=...) & Q(cargo=mawb)).
    raw_xml после прохода не хранится.
    """

    def __init__(self):
        # cargo_id → ({hawb_pk: _MsgLite}, {hawb_pk партии на момент снапшота})
        self._latest_by_cargo: dict[int, tuple[dict, set]] = {}
        # Ленивый кэш outbox-подач: (set hawb_id, set hawb_number). Строится
        # один раз за контекст (не за процесс — иначе realtime-путь получил бы
        # стейл по свежим подачам). Живёт только пока активен ed_status_batch.
        self._outbox: 'tuple[set, set] | None' = None

    def outbox_has(self, hawb) -> bool:
        """Есть ли по HAWB outbox-подача (FK hawb_id или в parsed_meta.hawbs)."""
        if self._outbox is None:
            self._outbox = _build_outbox_index()
        ids, nums = self._outbox
        return hawb.pk in ids or hawb.hawb_number in nums

    # ── шаг 1.5 ──

    def latest_for(self, hawb):
        """_MsgLite | None | _MISSING (HAWB нет в снапшоте — нужен fallback)."""
        entry = self._latest_by_cargo.get(hawb.mawb_id)
        if entry is None:
            entry = self._build_cargo(hawb.mawb_id)
        per_hawb, snapshot_ids = entry
        if hawb.pk not in snapshot_ids:
            # HAWB создана/перелинкована в партию ПОСЛЕ построения кэша
            # (авто-создание siblings, relink-крон) — снапшот её не знает.
            # Одиночный Q(hawb=hawb) нашёл бы FK-сообщения — сигналим
            # вызывающему упасть на одиночный путь.
            return _MISSING
        return per_hawb.get(hawb.pk)

    def _build_cargo(self, cargo_id: int) -> tuple[dict, set]:
        from cargo.models import AltaInboxMessage, HouseWaybill
        from django.db.models import Q

        hawbs = list(HouseWaybill.objects
                     .filter(mawb_id=cargo_id)
                     .values_list('pk', 'hawb_number'))
        hawb_ids = {pk for pk, _ in hawbs}
        latest: dict[int, _MsgLite] = {}
        qs = (AltaInboxMessage.objects
              .filter(Q(cargo_id=cargo_id) | Q(hawb_id__in=list(hawb_ids)))
              .exclude(msg_kind__in=_INSIGNIFICANT_KINDS)
              .values_list('pk', 'hawb_id', 'cargo_id', 'msg_kind',
                           'parsed_meta', 'prepared_at', 'received_at',
                           'raw_xml'))
        for m_pk, m_hawb_id, m_cargo_id, kind, pm, prep, recv, raw \
                in qs.iterator():
            lite = _MsgLite(m_pk, kind, pm, prep, recv)
            mentioned: set[int] = set()
            # FK-атрибуция: msg.hawb ∈ партии (как Q(hawb=hawb), без
            # условия на msg.cargo)
            if m_hawb_id in hawb_ids:
                mentioned.add(m_hawb_id)
            if m_cargo_id == cargo_id and raw:
                for pk, num in hawbs:
                    if num and num in raw:
                        mentioned.add(pk)
            for pk in mentioned:
                cur = latest.get(pk)
                if cur is None or lite._sort_key() > cur._sort_key():
                    latest[pk] = lite
        entry = (latest, hawb_ids)
        self._latest_by_cargo[cargo_id] = entry
        return entry


def _outbox_refs_ready() -> bool:
    """Заполнена ли денормализованная таблица AltaOutboxWaybill (бэкфилл прошёл).

    Поэтапное внедрение (safe):
    - таблицы ЕЩЁ НЕТ (миграция 0070 не применена) → OperationalError →
      возвращаем False → чтение идёт по старому parsed_meta-пути (= класс-1,
      проверенное поведение). Код можно деплоить/коммитить ДО миграции.
    - таблица есть, но пуста (бэкфилл не прогнан) → False → тот же fallback.
    - таблица заполнена → True → быстрый путь без raw_xml.
    """
    from django.db import OperationalError
    from cargo.models import AltaOutboxWaybill
    try:
        return AltaOutboxWaybill.objects.exists()
    except OperationalError:
        return False


def _build_outbox_index() -> 'tuple[set, set]':
    """(set hawb_id, set hawb_number) для всех outbox-подач.

    FK-часть — через колонку `hawb_id` (дёшево, индекс). Номера:
    - БЫСТРЫЙ ПУТЬ (после бэкфилла): из денормализованной AltaOutboxWaybill —
      только номера, БЕЗ raw_xml. Индексировано, дёшево.
    - FALLBACK (до бэкфилла, таблица пуста): старый проход по parsed_meta.
      ⚠ он тянет raw_xml до 4МБ на строку → память; временно, до бэкфилла.
    """
    from cargo.models import AltaOutboxObservation, AltaOutboxWaybill
    base = AltaOutboxObservation.objects.filter(msg_type__in=_SUBMISSION_TYPES)
    ids = set(base.filter(hawb_id__isnull=False)
              .values_list('hawb_id', flat=True))
    if _outbox_refs_ready():
        nums = set(AltaOutboxWaybill.objects
                   .filter(observation__msg_type__in=_SUBMISSION_TYPES)
                   .values_list('hawb_number', flat=True))
    else:
        nums = set()
        for pm in base.values_list('parsed_meta', flat=True).iterator():
            for hn in ((pm or {}).get('hawbs') or []):
                if hn:
                    nums.add(hn.strip())
    return ids, nums


def _has_resubmission(hawb, baseline_ts) -> bool:
    """Есть ли outbox-подача ПОЗЖЕ baseline_ts, относящаяся к этой HAWB.

    values_list('parsed_meta__hawbs') — json_extract на стороне SQLite:
    тянем только маленький массив номеров, а НЕ весь parsed_meta (в него
    агент кладёт полный raw_xml — до 4МБ на строку CMN.11335/11349;
    полная выборка была ~150+МБ и грелась на каждый rejected-HAWB).
    Запрос свежий per-call — никакого кэша, стейла нет."""
    from cargo.models import AltaOutboxObservation
    rows = (AltaOutboxObservation.objects
            .filter(msg_type__in=_SUBMISSION_TYPES,
                    prepared_at__gt=baseline_ts)
            .values_list('hawb_id', 'parsed_meta__hawbs'))
    for o_hawb_id, hawbs_list in rows.iterator():
        if o_hawb_id == hawb.pk:
            return True
        if hawb.hawb_number in (hawbs_list or []):
            return True
    return False


@contextlib.contextmanager
def ed_status_batch():
    """Контекст для массовых прогонов compute_ed_status (команды/кроны).

    Внутри контекста шаги 1.5/1.6 работают через пред-агрегацию per-cargo
    (см. _EdStatusBatchCache). Кэш живёт ТОЛЬКО до выхода из контекста —
    свежесть realtime-путей не страдает. Не использовать вокруг кода,
    который сам меняет inbox/outbox и тут же ждёт нового статуса.
    """
    token = _batch_ctx.set(_EdStatusBatchCache())
    try:
        yield
    finally:
        _batch_ctx.reset(token)


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
        # ⚠ .all() (prefetch-кэш), НЕ .filter(): любой .filter() по связи
        # игнорирует prefetch и уходит в БД отдельным запросом на КАЖДУЮ HAWB.
        # На батч-аудите (14.8к HAWB) это был главный пожиратель времени
        # (320с, N+1). Meta.ordering=['hawb','attempt_number'] → первый матч
        # по .all() = наименьший attempt_number, как у .filter().first().
        cur_attempt = next(
            (a for a in hawb.declaration_attempts.all()
             if a.declaration_number == cur_decl), None)
        if cur_attempt:
            if cur_attempt.status == 'RELEASED':
                main = 'Выпуск разрешен'
            elif cur_attempt.status == 'REJECTED':
                main = 'Отказано в выпуске'
            # FILED для current — релиз был от ДРУГОЙ (старой) ДТ,
            # не используем release_date. Падаем дальше в latest CMN.
        elif hawb.customs_status == 'RELEASED' and hawb.release_date:
            # Sibling ДТЭГ: current ДТ есть, выпуск в БД есть (RELEASED +
            # release_date проброшены с головной накладной), но собственной
            # HawbDeclarationAttempt под эту ДТ НЕ создалось. Без этой ветки
            # шаг 1 не находит attempt → падает на latest CMN, а туда для
            # multi-HAWB ДТЭГ прилетает rejected CMN.11350 по ДРУГОЙ ДТ той же
            # партии (упоминает нашу накладную в raw_xml как sibling) → ложно
            # «Идет проверка» на реально выпущенной накладной.
            # Кейс 141-53626160 (23.07.2026): 10289221453/10289234703 — ДТ
            # 0026559 выпущена 17.07, а rejected 0026862/0026874 от 20.07
            # перебивал. Защита от кейса переподачи: там customs_status !=
            # RELEASED (REJECTED/пусто), поэтому эта ветка их не трогает.
            main = 'Выпуск разрешен'
    elif hawb.release_date:
        # Нет current decl, но есть release_date — легаси-кейс,
        # сохраняем старое поведение.
        main = 'Выпуск разрешен'

    # 1.5. Если финальных нет — смотрим последнее значимое CMN-сообщение.
    # Расширяем поиск: либо FK hawb=hawb, либо msg.raw_xml упоминает
    # hawb_number И msg.cargo=hawb.mawb. Это нужно для multi-HAWB ДТ:
    # CMN.11350 может быть привязан только к одной HAWB (matched), но
    # упоминает всех siblings в raw_xml.
    batch = _batch_ctx.get()
    latest = _MISSING
    if batch is not None and hawb.mawb_id and hawb.hawb_number:
        # Батч-режим: пред-агрегация per-cargo вместо per-HAWB LIKE.
        # _MISSING = HAWB не было в снапшоте (создана/перелинкована после
        # построения кэша) → одиночный путь ниже.
        latest = batch.latest_for(hawb)
    if latest is _MISSING:
        from django.db.models import Q
        cond = Q(hawb=hawb)
        if hawb.mawb_id and hawb.hawb_number:
            cond = cond | (Q(raw_xml__icontains=hawb.hawb_number)
                           & Q(cargo=hawb.mawb))
        msgs = AltaInboxMessage.objects.filter(
            cond,
        ).exclude(msg_kind__in=_INSIGNIFICANT_KINDS)
        # '-pk' — детерминированный тай-брейк при равных датах (в батч-пути
        # так же, см. _MsgLite._sort_key).
        latest = msgs.order_by('-prepared_at', '-received_at', '-pk').first()
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
            # Связь outbox с HAWB: либо FK hawb_id, либо в parsed_meta.hawbs.
            # Лёгкий свежий запрос per-call (см. _has_resubmission) — общий
            # для одиночного и батч-режима.
            has_resubmission = _has_resubmission(hawb, baseline_ts)
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
        if batch is not None:
            # Батч-режим: единый кэш (FK hawb_id + hawb_number) за ОДИН проход,
            # живёт только в контексте. Раньше тут был per-HAWB
            # `filter(hawb=hawb).exists()` — N+1 SQL на каждую HAWB без статуса
            # (свежеподанные, которых на аудите тысячи).
            has_outbox = batch.outbox_has(hawb)
        else:
            # Realtime-путь (вне ed_status_batch): FK — свежим запросом (стейл
            # недопустим для только что поданной HAWB).
            outbox_qs = AltaOutboxObservation.objects.filter(
                msg_type__in=_SUBMISSION_TYPES)
            has_outbox = outbox_qs.filter(hawb=hawb).exists()
            if not has_outbox and _outbox_refs_ready():
                # Точечный запрос по индексированному номеру — мгновенный,
                # raw_xml не трогает (это и есть цель денормализации).
                from cargo.models import AltaOutboxWaybill
                has_outbox = AltaOutboxWaybill.objects.filter(
                    observation__msg_type__in=_SUBMISSION_TYPES,
                    hawb_number=hawb.hawb_number).exists()
            elif not has_outbox:
                # Fallback до бэкфилла: process-кэш номеров (как в оригинале).
                nums = getattr(compute_ed_status, '_outbox_nums_cache', None)
                if nums is None:
                    nums = _build_outbox_index()[1]
                    compute_ed_status._outbox_nums_cache = nums
                if hawb.hawb_number in nums:
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
        # .all() (prefetch), не .filter().exists() (N+1 на каждую HAWB).
        has_filed_attempt = any(
            a.declaration_number == cur_decl and a.status == 'FILED'
            for a in hawb.declaration_attempts.all())
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
            # len(.all()) (prefetch), не .count() (N+1 SELECT COUNT на HAWB).
            n_req = len(hawb.customs_requests.all())
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
