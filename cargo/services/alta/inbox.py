"""Inbox: входящие ЭД-сообщения от таможни.

Точка входа — `dispatch(msg)`, вызывается из view `api_alta_inbox_post`
сразу после `update_or_create` записи AltaInboxMessage. Делает три шага:
1. Подбирает HAWB по WayBillNumber (raw) → HouseWaybill.hawb_number.
2. Применяет статусный маппинг через HouseWaybill.change_customs_status().
3. Создаёт HawbWorkflowEvent для таймлайна и триггерит sheets writeback
   в фоновом потоке.

Точные ED-коды добавляются в MSG_KIND_MAP после получения реальных .gz
примеров. До тех пор все неизвестные коды попадают в kind='info' —
сообщение сохраняется для visibility, но статус HAWB не меняется.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Optional

from django.utils import timezone

from cargo.models import AltaInboxMessage, Cargo, HawbWorkflowEvent, HouseWaybill


logger = logging.getLogger('cargo.alta.inbox')


def _autocreate_disabled() -> bool:
    """Process-wide kill-switch для auto-create EXPORT HouseWaybill веток.
    Включается env ALTA_INBOX_AUTOCREATE_DISABLED=1 на время backfill,
    чтобы старые legacy raw_xml не плодили фейковые HAWB."""
    import os
    return os.environ.get('ALTA_INBOX_AUTOCREATE_DISABLED', '').strip().lower() \
        in ('1', 'true', 'yes', 'on')


# ─── маппинг MessageType на наш semantic kind ──
# Из реальных .gz из C:\GTDSERV\ED\IN.
MSG_KIND_MAP: dict[str, str] = {
    'CMN.00003': 'info',         # ArchResult — ACK от gateway: «обработано»
    'CMN.11010': 'released',     # ED_Container «Выпуск товаров разрешен» (DecisionCode 10)
    'CMN.11309': 'released',     # ExpressNotification — уведомление о выпуске
                                 # (ResolutionDescription="Выпуск товаров разрешен",
                                 # DecisionCode=10). Если DecisionCode=90 — рефайн в rejected.
    'CMN.11310': 'info',         # ACK / customs mark без явного решения
    'CMN.11350': 'released',     # ExpressCargoDeclarationCustomMark — отметка таможни.
                                 # DecisionCode 10=выпуск, 90=отказ. Уточняется в classify().
    'CMN.11354': 'withdrawn',    # ExpressCargoDeclarationCustomMark — разрешение
                                 # на отзыв ДТ (DecisionCode=40). Это ответ таможни
                                 # на нашу CMN.11011 (запрос отзыва). HAWB не несёт,
                                 # связка через initial_envelope → CMN.11011 outbox →
                                 # mcd_id → CMN.11335 outbox с hawbs.
    'CMN.11314': 'info',         # Закрытие процедуры (DO1Close)
    'CMN.13021': 'info',         # DO1KeepLimits — лимит хранения / размещение на СВХ
    'CMN.13029': 'svh_placed',   # WHDocInventory — представление в таможню с MAWB.
                                 # Якорь (DocumentID) — для связи с CMN.13010.
    'CMN.13010': 'svh_do1_registered',  # DORegInfo — РЕАЛЬНАЯ регистрация ДО1.
                                        # Дата размещения + рег.номер ДО1. Связь с партией
                                        # через RefDocumentID → CMN.13029.DocumentID → Cargo.
    'CMN.13014': 'svh_do2_registered',  # WHGoodOut — отчёт о ВЫПУСКЕ груза со СВХ (ДО2).
                                        # Рег.номер ДО2 + дата+время выпуска (SendDate/Time).
                                        # Связь с Cargo через MAWB в TransportDoc или ссылку
                                        # на ДО-1 в Comments.
    'ED.11003':  'customs_request',     # ReqInventoryDoc — запросы документов от инспектора.
                                        # Один envelope может содержать N <RequestedDoc>
                                        # блоков (запросов). InitialEnvelopeID → наша
                                        # CMN.11349, Position → конкретный товар → HAWB.
                                        # (Alta-ГТД часто переименовывает файл в Alta_MY11003,
                                        # но MessageType в теле всегда ED.11003.)
    'MY.11003':  'customs_request',     # То же что ED.11003 но в новом формате XML
                                        # Альта-ГТД 5.27+. MessageType явно MY.11003.
    'CMN.11337': 'registered',          # Извещение о регистрации ДТ (CMN.11337):
                                        # таможня присвоила номер декларации. parsed_meta
                                        # содержит CustomsCode/RegistrationDate/GTDNumber —
                                        # достаточно для recompute_declaration.
                                        # InitialEnvelopeID связывает с нашей подачей.
    'CMN.11001': 'registered',          # ED-приём (приём ЭД) — таможня присвоила
                                        # рег.номер. Аналогично CMN.11337 для классических
                                        # ДТ (CMN.11024) и других не-ДТЭГ-форм.
    'CMN.11002': 'hold',                # «Начато оформление» / «Уведомление о начале
                                        # проверки ДТ» — в Альте отображается как
                                        # «Идет проверка» (документальная). Физический
                                        # досмотр — отдельный тип сообщения (kind=
                                        # 'examination'). Refine на rejected/released
                                        # происходит в classify() если в parsed_meta
                                        # есть decision_code/design_code.
    'CMN.11062': 'registration_rejected',  # Отказ в регистрации декларации
                                            # (терминальное состояние подачи).
                                            # Декларация НЕ зарегистрирована,
                                            # рег.номер не присвоен.
}

# Лицензия нашего СВХ (СДЭК-ГЛОБАЛ). СВХ-сообщения с другими лицензиями
# приходят валом (рабочий сервер обслуживает много складов), но нас интересуют
# только наши. Фильтр в classify() переводит чужое в 'info'.
OUR_WAREHOUSE_LICENSE = '10001/060324/10009/1'

# DecisionCode → конкретный kind для типов где он есть в теле.
# 10 — выпуск, 70 — запрос документов, 90 — отказ.
# (40 — отзыв декларации, обычно в Design а не DecisionCode.)
DECISION_CODE_KIND: dict[str, str] = {
    '10': 'released',
    '11': 'released',
    '40': 'withdrawn',    # Разрешение на отзыв ДТ (CMN.11354 consignment-блок).
                          # На CMN.11350 не встречается — Альта использует только
                          # 10/11/70/90/91 для классических ДТ-решений.
    '70': 'hold',         # Запрос дополнительных документов и сведений
    '90': 'rejected',
    '91': 'rejected',
}

# GoodsShipment_HouseShipment\Design — более точный код решения чем DecisionCode.
# Если Design=40 — это отзыв декларации, ДТ-номер становится недействительным
# и должен быть НЕ записан / стёрт.
DESIGN_CODE_KIND: dict[str, str] = {
    '10': 'released',      # выпуск товаров
    '11': 'released',      # выпуск с условиями
    '40': 'withdrawn',     # отзыв декларации
    '90': 'rejected',
    '91': 'rejected',
}


def classify(msg_type: str, parsed_meta: Optional[dict] = None) -> str:
    """MessageType (+ опц parsed_meta из тела) → kind.

    Приоритет: consignments (per-HAWB) > Design > DecisionCode > ResolutionDescription > MessageType.
    Разные типы сообщений несут результат таможни в разных полях, поэтому
    проверяем все три семантически-полных индикатора.

    Неизвестные коды → 'info' (статус не меняем).
    """
    base = MSG_KIND_MAP.get((msg_type or '').strip(), 'info')
    if not parsed_meta:
        return base

    # СВХ-ветка: refine на свою лицензию. Чужие склады отсекаем в info, чтобы
    # не загромождать UI и не пытаться матчить их MAWB к нашим Cargo.
    if base in ('svh_placed', 'svh_do1_registered', 'svh_do2_registered'):
        lic = (parsed_meta.get('svh_warehouse_license') or '').strip()
        if lic and lic != OUR_WAREHOUSE_LICENSE:
            return 'info'
        # ДО2 (FormReport=2) тоже приходит как CMN.13010 — пока не интересует
        # (полноценный ДО2 у нас отдельным типом CMN.13014).
        if base == 'svh_do1_registered':
            form = (parsed_meta.get('svh_do1_form_report') or '').strip()
            if form and form != '1':
                return 'info'
        return base

    # CMN.11350 с consignment-блоками per-HAWB. Один XML может содержать
    # СМЕСЬ решений (часть HAWB — выпуск, часть — отказ). Для msg_kind
    # (= label в UI/фильтрах + якорь recompute_declaration который ищет
    # msg_kind__in=('released','withdrawn')) берём ДОМИНАНТНЫЙ kind:
    # released > withdrawn > rejected > examination > hold > info.
    # Фактическое per-HAWB решение применяется в apply_consignment_decisions.
    consignments = parsed_meta.get('consignments') or []
    if consignments:
        kinds = {DECISION_CODE_KIND.get((c.get('decision_code') or '').strip(), 'info')
                 for c in consignments}
        for priority_kind in ('released', 'withdrawn', 'rejected',
                              'examination', 'hold'):
            if priority_kind in kinds:
                return priority_kind
        return 'info'

    # 1. Design — самый точный код по конкретной ДТ (когда есть)
    dsn = (parsed_meta.get('design_code') or '').strip()
    if dsn:
        return DESIGN_CODE_KIND.get(dsn, base)

    # 2. DecisionCode — для любых типов где он присутствует
    dc = (parsed_meta.get('decision_code') or '').strip()
    if dc in DECISION_CODE_KIND:
        return DECISION_CODE_KIND[dc]

    # 3. ResolutionDescription — текстовый маркер (русский) для типов без
    #    числового кода. Заведомо positive/negative фразы.
    rt = (parsed_meta.get('resolution_text') or '').lower()
    if rt:
        if 'выпуск товаров разрешен' in rt or 'разрешен выпуск' in rt:
            return 'released'
        if 'отзыв декларации' in rt or 'декларация отозвана' in rt:
            return 'withdrawn'
        if 'отказано в выпуске' in rt or 'отказ в выпуске' in rt:
            return 'rejected'

    return base

STATUS_FROM_KIND: dict[str, str] = {
    'registered':  'FILED',
    'released':    'RELEASED',
    'rejected':    'REJECTED',
    'withdrawn':   'WITHDRAWN',  # отзыв декларации — инициатива нашего декларанта,
                                 # CMN.11354 = разрешение таможни. Отличается от
                                 # REJECTED (отказ — решение таможни).
    'examination': 'EXAMINATION',
    'hold':        'HOLD',
    'registration_rejected': 'REJECTED',  # отказ в регистрации = REJECTED
                                          # (юзер видит как «Считается не поданной»)
}

# HawbWorkflowEvent.event_type для записи в таймлайн (event_type у нас
# открытый, дополнительные значения допустимы — но используем существующие
# где можно).
EVENT_TYPE_FROM_KIND: dict[str, str] = {
    'registered':  'DECLARATION_ISSUED',
    'released':    'OTHER',  # отдельного choice нет; различаем через msg_kind
    'rejected':    'OTHER',
    'examination': 'CUSTOMS_REQUEST',
    'hold':        'CUSTOMS_REQUEST',
    'svh_placed':  'OTHER',
    'info':        'OTHER',
    'registration_rejected': 'OTHER',
}


def match(msg: AltaInboxMessage) -> tuple[Optional[Cargo], Optional[HouseWaybill]]:
    """Подобрать Cargo и/или HAWB для входящего сообщения.

    На рабочем сервере Альта обслуживает много workflow помимо CargoTrack,
    поэтому 99%+ inbox-сообщений нам не принадлежат. Матчинг возможен только
    через идентификаторы, которые мы сами породили при отправке.

    В IndPost-flow Альта сама строит исходящие пакеты с собственными
    EnvelopeID — мы их не контролируем. Связь восстанавливаем через
    `AltaOutboxObservation` (записи наблюдаемых 538134^* файлов).

    Стратегия:
    1. parsed_meta['initial_envelope'] → AltaQueueItem.envelope_id → hawb
       (если мы сами через свой queue послали что-то типа ED.1002018 — редкий путь)
    2. parsed_meta['initial_envelope'] → AltaOutboxObservation.envelope_id
       → (cargo, hawb). Основной путь.
    3. Построить customs_declaration_number → ищем HAWB или Cargo с этим
       номером ДТ (для повторных и кросс-кросс-вариантов).
    4. waybill_number_raw → HouseWaybill (fallback, наблюдений не было).

    Возвращает (cargo, hawb) — любой может быть None. Оба None — чужое.
    """
    from cargo.models import AltaQueueItem, AltaOutboxObservation

    parsed = msg.parsed_meta or {}

    # 1. Через наш собственный queue (для редких форматов с envelope_wrap)
    init = (parsed.get('initial_envelope') or '').strip()
    if init:
        q = (
            AltaQueueItem.objects
            .filter(envelope_id__iexact=init)
            .exclude(hawb=None)
            .select_related('hawb', 'hawb__mawb')
            .first()
        )
        if q and q.hawb:
            return (q.hawb.mawb, q.hawb)

        # 2. Через наблюдение исходящих копий Альты (основной путь для IndPost)
        obs = (
            AltaOutboxObservation.objects
            .filter(envelope_id__iexact=init)
            .select_related('cargo', 'hawb')
            .first()
        )
        if obs and (obs.cargo or obs.hawb):
            cargo = obs.cargo or (obs.hawb.mawb if obs.hawb and obs.hawb.mawb_id else None)
            return (cargo, obs.hawb)
        # 2.5. Auto-create EXPORT HAWB по hawbs списку из outbox parsed_meta.
        # Сценарий: старый агент для CMN.11024/11335 не сохранил raw_xml,
        # поэтому _apply_export_outbox не создал HAWB. Теперь приходит
        # CMN.11337/11001 с initial_envelope ссылающимся на тот outbox.
        # Создаём HAWB(EXPORT) и возвращаем.
        # ВАЖНО: создаём только если outbox raw_xml подтверждает ЭК
        # (DeclarationKindCode='ЭК' для CMN.11335/11349, CustomsProcedure='ЭК'
        # для CMN.11024). Если raw_xml пустой — НЕ создаём, мы не знаем
        # export или import (legacy agent). Импортные HAWB у нас живут в
        # таблице «Общее», их юзер вводит вручную.
        if obs and obs.msg_type in ('CMN.11024', 'CMN.11023',
                                    'CMN.11335', 'CMN.11349'):
            pm = obs.parsed_meta or {}
            hawb_list = pm.get('hawbs') or []

            # Сначала смотрим — может HAWB уже существует в БД (например
            # создана вручную через add_export_hawb). Тогда возвращаем её
            # независимо от raw_xml.
            for hn in hawb_list:
                hn = str(hn).strip()
                if not hn:
                    continue
                existing = HouseWaybill.objects.filter(
                    hawb_number__iexact=hn).first()
                if existing:
                    return (existing.mawb, existing)

            # HAWB в БД нет → пытаемся auto-create, но только если raw_xml
            # подтверждает ЭК. Без raw_xml не знаем экспорт это или импорт
            # — пропускаем (импортные мы не хотим в экспортной вкладке).
            raw_xml = pm.get('raw_xml') or ''
            is_export = False
            if raw_xml:
                from cargo.services.alta.xml_extract import (
                    parse_cmn_11335, parse_cmn_11024, parse_cmn_11349_meta,
                )
                try:
                    if obs.msg_type in ('CMN.11024', 'CMN.11023'):
                        r = parse_cmn_11024(raw_xml)
                        is_export = (r.get('customs_procedure') or '').strip() == 'ЭК'
                    else:
                        r = parse_cmn_11335(raw_xml)
                        is_export = (r.get('declaration_kind') or '').strip() == 'ЭК'
                except Exception:
                    logger.exception(
                        'match: parse outbox raw_xml failed for %s', obs.envelope_id)
                    is_export = False
            if not is_export:
                return (None, None)  # импорт/неизвестно → пропускаем

            if _autocreate_disabled():
                # Kill-switch для backfill: не создаём фейковые EXPORT HAWB
                # по упоминаниям в старых raw_xml. Возвращаем (None, None),
                # как будто HAWB не найден.
                logger.info(
                    'match: auto-create skipped (ALTA_INBOX_AUTOCREATE_DISABLED) '
                    'for envelope %s, hawbs=%s', init, hawb_list)
                return (None, None)

            for hn in hawb_list:
                hn = str(hn).strip()
                if not hn:
                    continue
                try:
                    new_h = HouseWaybill.objects.create(
                        hawb_number=hn,
                        shipment_type='EXPORT',
                        logistics_status='EXPORT_CUSTOMS',
                        cdek_number='',
                    )
                    logger.info(
                        'match: auto-created HAWB %s (EXPORT) via initial_envelope %s',
                        hn, init)
                    return (None, new_h)
                except Exception:
                    logger.exception('match: auto-create HAWB %s failed', hn)

        # 2.6. Sibling-mcd_id — для CMN.11354 (разрешение отзыва).
        # CMN.11354.initial_envelope ссылается на наш CMN.11011 outbox (запрос
        # отзыва). У CMN.11011 raw_xml не сохраняется (legacy агент), поэтому
        # hawbs там пуст. НО parsed_meta.mcd_id — это Master Customs Declaration
        # ID, общий для всей ДТЭГ (CMN.11335 подача, CMN.11011 отзыв и др.).
        # Через тот же mcd_id находим sibling CMN.11335 (наша ПТДЭГ-подача), у
        # которой hawbs есть.
        if obs:
            ob_pm = obs.parsed_meta or {}
            mcd_id = (ob_pm.get('mcd_id') or '').strip()
            if mcd_id:
                sib_qs = (AltaOutboxObservation.objects
                          .filter(parsed_meta__mcd_id=mcd_id)
                          .exclude(pk=obs.pk)
                          .select_related('cargo', 'hawb'))
                for sib in sib_qs:
                    sib_hawbs = (sib.parsed_meta or {}).get('hawbs') or []
                    for hn in sib_hawbs:
                        hn = str(hn).strip()
                        if not hn:
                            continue
                        h = HouseWaybill.objects.filter(
                            hawb_number__iexact=hn).first()
                        if h:
                            return (h.mawb, h)
                    # Если у sibling proставлен FK напрямую — тоже подходит.
                    if sib.hawb:
                        return (sib.hawb.mawb, sib.hawb)
                    if sib.cargo and not sib.hawb:
                        # Cargo есть, hawb нет — возвращаем только cargo.
                        # apply_consignment_decisions сам разберётся per-HAWB
                        # по mcd-связке когда waybills в сообщении пусты —
                        # для withdrawn это значит «отзыв всей декларации».
                        return (sib.cargo, None)

    # 2.7. providing_hawbs — для CMN.11001 (ProvidingIndicationList).
    # initial_envelope в нём отсутствует, но в теле явно перечислены HAWB
    # с DocCode=02021. Берём первый существующий HAWB.
    providing_hawbs = parsed.get('providing_hawbs') or []
    for hn in providing_hawbs:
        hn = str(hn).strip()
        if not hn:
            continue
        h = HouseWaybill.objects.filter(hawb_number__iexact=hn).first()
        if h:
            return (h.mawb, h)

    # 2.8. consignments.waybills — для CMN.11350 (ExpressCargoDeclarationCustomMark).
    # Когда сообщение пришло БЕЗ initial_envelope (например через
    # db_reconcile из Postgres БД Альты или новый агент без envelope-wrap),
    # waybill_number_raw пуст и шаги 1-2.7 матча не находят HAWB. При этом
    # parsed_meta.consignments[*].waybills содержит точный список HAWB.
    consignments_match = parsed.get('consignments') or []
    for cons in consignments_match:
        for hn in (cons.get('waybills') or []):
            hn = str(hn).strip()
            if not hn:
                continue
            h = HouseWaybill.objects.filter(hawb_number__iexact=hn).first()
            if h:
                return (h.mawb, h)

    # 3. По собранному номеру ДТ
    decl = _build_declaration_number(parsed)
    if decl:
        hawb = HouseWaybill.objects.filter(customs_declaration_number=decl).first()
        if hawb:
            return (hawb.mawb, hawb)
        cargo = Cargo.objects.filter(customs_declaration_number=decl).first()
        if cargo:
            return (cargo, None)

    # 4. Fallback — WayBillNumber из XML
    wn = (msg.waybill_number_raw or '').strip()
    if wn:
        hawb = HouseWaybill.objects.filter(hawb_number__iexact=wn).first()
        if hawb:
            return (hawb.mawb, hawb)

    # 5. Финальный fallback — поиск 10-значных HAWB-номеров напрямую в raw_xml.
    # Применяется для классических ДТ-релизов (CMN.11010 от db_reconcile), где
    # специализированный парсер не извлёк consignments/initial_envelope:
    # parsed_meta минимальный, а HAWB-номера лежат в теле декларации внутри
    # <PrDocumentNumber>...</PrDocumentNumber>. Berём первый существующий
    # HAWB — для multi-HAWB релиза этого достаточно: recompute_declaration
    # сама пересчитает siblings (project_decl_propagation).
    raw_xml = (msg.raw_xml or '')
    if raw_xml and (msg.msg_type or '') in ('CMN.11010', 'CMN.11309',
                                              'CMN.11341', 'CMN.11337',
                                              'CMN.11001', 'CMN.11350'):
        import re as _re_raw
        for hn in sorted(set(_re_raw.findall(r'(102\d{8})', raw_xml))):
            h = HouseWaybill.objects.filter(hawb_number__iexact=hn).first()
            if h:
                return (h.mawb, h)

    return (None, None)


# Обратная совместимость для существующих импортов (если есть).
def match_hawb(msg: AltaInboxMessage) -> Optional[HouseWaybill]:
    _, hawb = match(msg)
    return hawb


def _build_declaration_number(parsed_meta: dict) -> str:
    """Собирает «10005020/200526/0018179» из CustomsCode + RegistrationDate + GTDNumber."""
    cc = (parsed_meta.get('customs_code') or '').strip()
    rd = (parsed_meta.get('registration_date') or '').strip()
    gn = (parsed_meta.get('gtd_number') or '').strip()
    if not (cc and rd and gn):
        return ''
    # RegistrationDate приходит как '2026-05-20' → форматируем в 200526
    try:
        y, m, d = rd.split('-')
        rd_short = f'{d}{m}{y[2:]}'
    except ValueError:
        rd_short = rd
    return f'{cc}/{rd_short}/{gn}'


def recompute_declaration(cargo: Optional[Cargo],
                          hawb: Optional[HouseWaybill]) -> list[HouseWaybill]:
    """Пересчитывает customs_declaration_number из всей истории inbox-сообщений.

    Работает по конкретной HAWB. Ищет released/withdrawn сообщения двумя путями:
    1. msg.hawb=X — прямая привязка из dispatch.
    2. raw_xml содержит X.hawb_number И msg.cargo=X.mawb — для release-сообщений
       одной ДТ, покрывающей несколько HAWB одной партии: в CMN.11350 у Альты
       лежит список из N <PrDocumentNumber>, и наш match привязал msg только
       к одной HAWB. Раз HAWB-номер встречается в raw_xml того сообщения и
       партия совпадает — этой HAWB тоже relevant.

    Берёт самое свежее по prepared_at:
    - released → пишет ДТ из его parsed_meta
    - withdrawn → стирает ДТ

    Возвращает список HAWB у которых реально изменился номер — для sheets writeback.
    """
    if not hawb:
        return []

    from django.db.models import Q
    cond = Q(hawb=hawb)
    if hawb.mawb_id and hawb.hawb_number:
        # Фильтр по Cargo защищает от случайных совпадений номеров между
        # разными партиями (HAWB-номера не уникальны глобально).
        cond = cond | (Q(raw_xml__icontains=hawb.hawb_number) & Q(cargo=hawb.mawb))

    # Рег.номер ДТ записываем как только он впервые появится в любом значимом
    # сообщении от таможни (released/rejected/examination/hold/withdrawn/
    # registered) — раньше ограничивались released/withdrawn, что задерживало
    # появление номера в Sheets до момента выпуска. Withdrawn по-прежнему
    # стирает номер. Если у latest сообщения нет GTDNumber — ищем любое более
    # раннее с непустым GTDNumber.
    qs = AltaInboxMessage.objects.filter(
        cond,
    ).exclude(msg_kind__in=('info', 'svh_placed',
                            'svh_do1_registered', 'svh_do2_registered',
                            'customs_request'))
    latest = qs.order_by('-prepared_at', '-received_at').first()
    if not latest:
        return []

    # Защита от восстановления decl после REJECTED:
    # При REJECTED decl должен быть пустым (декларация анулирована).
    # Восстанавливаем только если пришёл CMN.11337/11001 ОТ НОВОЙ переподачи
    # (latest.msg_kind == 'registered' с новым GTDNumber).
    cur_status = HouseWaybill.objects.filter(pk=hawb.pk).values_list(
        'customs_status', flat=True).first() or ''
    if cur_status == 'REJECTED':
        # Если latest сообщение — финал отказа/отзыва, decl не восстанавливаем
        # и принудительно стираем (на случай если sweeper уже записал).
        if latest.msg_kind in ('rejected', 'withdrawn'):
            HouseWaybill.objects.filter(pk=hawb.pk).update(
                customs_declaration_number='', filed_date=None)
            return []
        newer_final = AltaInboxMessage.objects.filter(
            cond,
            msg_kind__in=('released', 'rejected', 'withdrawn'),
            prepared_at__gt=latest.prepared_at,
        ).exists()
        if newer_final:
            # Latest msg исторически старше финала — skip восстановление.
            return []

    if latest.msg_kind == 'withdrawn':
        target_decl = ''
    else:
        target_decl = _build_declaration_number(latest.parsed_meta or {})
        if not target_decl:
            # Latest без GTDNumber — поднимем самое свежее сообщение в очереди
            # у которого номер есть.
            for m in qs.order_by('-prepared_at', '-received_at'):
                t = _build_declaration_number(m.parsed_meta or {})
                if t:
                    target_decl = t
                    latest = m
                    break
            if not target_decl:
                return []

    from django.db import transaction
    extra_touched: list = []
    with transaction.atomic():
        current = HouseWaybill.objects.filter(pk=hawb.pk).values_list(
            'customs_declaration_number', flat=True).first() or ''
        # Пропагацию decl на siblings делаем В ЛЮБОМ случае (даже если у
        # текущей hawb decl уже совпал) — siblings могут отставать.
        if target_decl:
            extra_touched = _sync_decl_via_outbox(hawb, target_decl, latest)
        if current == target_decl:
            return extra_touched
        HouseWaybill.objects.filter(pk=hawb.pk).update(
            customs_declaration_number=target_decl)
        # Регистрируем попытку подачи: новый decl_number → новая попытка.
        _register_attempt(hawb, target_decl)

        # filed_date: дата подачи декларации = дата регистрации в таможне
        # (parsed_meta['registration_date'] из CMN-релиза). Ставим ОДИН раз
        # на пустое поле, через прямой UPDATE (минуя save()-автоочистки).
        # Writeback в Sheets отдельно — потому что direct UPDATE не дёргает
        # post_save сигнал.
        # TODO: registration_date — это ТОЛЬКО дата (без времени). Время
        # подачи можно достать только из отдельного CMN-регистрации
        # (CMN.11335 или подобный), если такой приходит. Пока 00:00:00.
        if target_decl:
            reg_date_str = (latest.parsed_meta or {}).get('registration_date') or ''
            if reg_date_str:
                from django.utils.dateparse import parse_date
                from datetime import datetime as _dt, time as _dt_time
                d = parse_date(reg_date_str)
                if d:
                    filed_dt = timezone.make_aware(_dt.combine(d, _dt_time(0, 0)))
                    HouseWaybill.objects.filter(
                        pk=hawb.pk, filed_date__isnull=True
                    ).update(filed_date=filed_dt)
            # Sync filed_date по всем HAWB с этой ДТ — берём минимум.
            # Полезно когда CMN.11023/11349 пришла для одного HAWB партии
            # (поле filed_date там было выставлено по реальному prepared_at),
            # а потом CMN.11350 проставила customs_declaration_number у других
            # HAWB этой же ДТ → нужно скопировать filed_date соседям.
            _sync_filed_date_by_declaration(target_decl)

    return [hawb] + extra_touched


def _sync_decl_via_outbox(hawb: HouseWaybill, target_decl: str,
                          latest_msg) -> list[HouseWaybill]:
    """Пропагирует target_decl на siblings HAWB одной декларации.

    Защита #1 (главная): если у latest_msg есть parsed_meta.initial_envelope,
    ищем outbox СТРОГО по этому envelope_id (якорь от таможни на наш
    конкретный CMN.11349/CMN.11023 этой подачи). Это исключает протекание
    decl новой подачи на siblings другой (старой) подачи той же партии
    через общий HAWB-пересечение.

    Защита #2 (страховка): перед update sibling — пропускаем тех, кто уже
    RELEASED с другим валидным decl (значит принадлежит другой подаче этой
    партии, текущий target_decl не должен на него протечь).

    Возвращает список изменённых HAWB.
    """
    from cargo.models import AltaOutboxObservation

    initial_env = ''
    if latest_msg:
        initial_env = ((latest_msg.parsed_meta or {})
                       .get('initial_envelope') or '').strip()

    sibling_set: set = set()
    if initial_env:
        # Защита #1: якорь по envelope_id — single hit на наш outbox.
        obs = AltaOutboxObservation.objects.filter(
            envelope_id=initial_env).first()
        if obs:
            pm = obs.parsed_meta or {}
            hawbs = pm.get('hawbs') or []
            for hn in hawbs:
                if hn != hawb.hawb_number:
                    sibling_set.add(hn)
    else:
        # Fallback: исторический поиск по hawbs membership.
        obs_list = AltaOutboxObservation.objects.filter(
            msg_type__in=('CMN.11023', 'CMN.11335',
                          'CMN.11024', 'CMN.11349'),
        )
        for o in obs_list:
            pm = o.parsed_meta or {}
            hawbs = pm.get('hawbs') or []
            if hawb.hawb_number in hawbs:
                for hn in hawbs:
                    if hn != hawb.hawb_number:
                        sibling_set.add(hn)
    if not sibling_set:
        return []

    # registration_date для filed_date (только дата 00:00)
    reg_date_dt = None
    if latest_msg:
        reg_date_str = (latest_msg.parsed_meta or {}).get('registration_date') or ''
        if reg_date_str:
            from django.utils.dateparse import parse_date
            from datetime import datetime as _dt, time as _dt_time
            d = parse_date(reg_date_str)
            if d:
                reg_date_dt = timezone.make_aware(_dt.combine(d, _dt_time(0, 0)))

    # Если у hawb уже shipment_type='EXPORT' — siblings из outbox тоже
    # экспортные, можно auto-create при отсутствии.
    is_export_chain = (hawb.shipment_type or 'IMPORT').upper() == 'EXPORT'

    changed: list[HouseWaybill] = []
    autocreate_off = _autocreate_disabled()
    for hn in sibling_set:
        sib = HouseWaybill.objects.filter(hawb_number__iexact=hn).first()
        if not sib and is_export_chain and not autocreate_off:
            # Auto-create отсутствующего sibling как EXPORT_CUSTOMS.
            try:
                sib = HouseWaybill.objects.create(
                    hawb_number=hn,
                    shipment_type='EXPORT',
                    logistics_status='EXPORT_CUSTOMS',
                )
                logger.info(
                    'sync_decl: auto-created sibling HAWB %s (EXPORT) of %s',
                    hn, hawb.hawb_number)
            except Exception:
                logger.exception('sync_decl: create %s failed', hn)
                continue
        if not sib:
            # Kill-switch active или sibling не нашёлся и chain не EXPORT —
            # просто пропускаем, не вылетаем.
            continue
        # Привязка к той же партии (если у sibling нет mawb, а у hawb есть)
        if hawb.mawb_id and not sib.mawb_id:
            HouseWaybill.objects.filter(pk=sib.pk).update(mawb_id=hawb.mawb_id)
        cur = (sib.customs_declaration_number or '').strip()
        if cur == target_decl:
            continue
        # Защита #2: не перезаписываем decl у sibling который УЖЕ RELEASED
        # с другой валидной ДТ. Это значит он принадлежит к ДРУГОЙ подаче
        # этой партии (которая выпущена), и текущий decl не должен на него
        # протекать через общий HAWB-пересечение.
        sib_data = HouseWaybill.objects.filter(pk=sib.pk).values(
            'customs_status', 'customs_declaration_number',
            'release_date').first()
        if sib_data and sib_data['customs_status'] == 'RELEASED' \
                and sib_data['release_date'] is not None \
                and sib_data['customs_declaration_number'] \
                and sib_data['customs_declaration_number'] != target_decl:
            logger.info(
                '_sync_decl_via_outbox: skipping sibling %s '
                '(already RELEASED with different decl %s, target was %s)',
                sib.hawb_number, sib_data['customs_declaration_number'],
                target_decl)
            continue
        upd = {'customs_declaration_number': target_decl}
        if reg_date_dt and sib.filed_date is None:
            upd['filed_date'] = reg_date_dt
        HouseWaybill.objects.filter(pk=sib.pk).update(**upd)
        sib.refresh_from_db(fields=list(upd.keys()))
        _register_attempt(sib, target_decl)
        changed.append(sib)
    if changed:
        logger.info('decl propagated to %d siblings of %s (decl=%s)',
                    len(changed), hawb.hawb_number, target_decl)
    return changed


def _register_attempt(hawb: HouseWaybill, declaration_number: str,
                      status: str = 'FILED',
                      filed_date=None,
                      release_date=None,
                      rejected_date=None,
                      trigger_writeback: bool = True) -> None:
    """Регистрирует попытку подачи декларации.

    Идемпотентно по (hawb, declaration_number). При update НЕ затирает
    уже установленные даты пустыми.
    Триггерит writeback счётчика «Переподачи» в Sheets.
    """
    from cargo.models import HawbDeclarationAttempt
    decl = (declaration_number or '').strip()
    if not decl or not hawb:
        return
    attempt, created = HawbDeclarationAttempt.objects.get_or_create(
        hawb=hawb,
        declaration_number=decl,
        defaults={
            'status':        status,
            'filed_date':    filed_date or hawb.filed_date,
            'release_date':  release_date or hawb.release_date,
            'rejected_date': rejected_date,
        },
    )
    if created:
        # Назначаем порядковый номер: count existing уже включая текущую.
        n = HawbDeclarationAttempt.objects.filter(hawb=hawb).count()
        attempt.attempt_number = n
        attempt.save(update_fields=['attempt_number'])
        logger.info('attempt #%d for HAWB %s decl=%s status=%s',
                    n, hawb.hawb_number, decl, status)
    else:
        # Апдейтим только пустые поля (никогда не затираем существующие).
        upd = {}
        if status and attempt.status != status and status != 'FILED':
            upd['status'] = status
        if filed_date and not attempt.filed_date:
            upd['filed_date'] = filed_date
        if release_date and not attempt.release_date:
            upd['release_date'] = release_date
        if rejected_date and not attempt.rejected_date:
            upd['rejected_date'] = rejected_date
        if upd:
            for k, v in upd.items():
                setattr(attempt, k, v)
            attempt.save(update_fields=list(upd.keys()))
            logger.info('attempt #%d for HAWB %s decl=%s updated: %s',
                        attempt.attempt_number, hawb.hawb_number, decl, upd)
    if trigger_writeback:
        _writeback_attempt_count_for_hawb(hawb)


def _writeback_attempt_count_for_hawb(hawb: HouseWaybill) -> None:
    """Триггерит запись счётчика «Переподачи» в Sheets для одной HAWB."""
    def _run():
        try:
            from cargo.services.sheets.writeback import (
                batch_write_attempts_count_for_hawbs, signals_suppressed,
            )
            if signals_suppressed():
                return
            batch_write_attempts_count_for_hawbs([hawb])
        except ImportError:
            pass
        except Exception:
            logger.exception('attempts_count writeback failed for HAWB %s',
                             hawb.hawb_number)
    threading.Thread(target=_run, daemon=True).start()


def _sync_filed_date_by_declaration(decl_number: str) -> None:
    """Для всех HAWB с этой ДТ — установить filed_date наиболее точный.

    Стратегия: если в группе есть хоть одно значение с НЕнулевым временем
    суток (например 13:50:40, источник = CMN.11023/11349.prepared_at) — берём
    его минимум среди таких. Только если ВСЕ значения = 00:00:00 (приходят
    из CMN.11350.registration_date — только дата) — берём min из них.

    Это устраняет «кражу точности»: раньше min([00:00, 13:50]) = 00:00
    распространялся на всю ДТ, теряя реальное время подачи.

    Идемпотентно: если у всех уже наилучший выбор — no-op.
    """
    decl = (decl_number or '').strip()
    if not decl:
        return
    hawbs = list(HouseWaybill.objects.filter(
        customs_declaration_number=decl
    ).only('pk', 'filed_date', 'hawb_number'))
    if len(hawbs) < 2:
        return
    dates = [h.filed_date for h in hawbs if h.filed_date]
    if not dates:
        return
    from django.utils import timezone as _tz
    def _precise(d):
        local = _tz.localtime(d) if _tz.is_aware(d) else d
        return bool(local.hour or local.minute or local.second or local.microsecond)
    precise = [d for d in dates if _precise(d)]
    min_date = min(precise) if precise else min(dates)
    affected = [h for h in hawbs if h.filed_date != min_date]
    for h in affected:
        HouseWaybill.objects.filter(pk=h.pk).update(filed_date=min_date)
    if affected:
        logger.info('filed_date sync by ДТ %s: %d HAWB → %s',
                    decl, len(affected), min_date)
        try:
            from cargo.services.sheets.writeback import (
                batch_write_filed_dates_for_hawbs, signals_suppressed,
            )
            if not signals_suppressed():
                for h in affected:
                    h.refresh_from_db(fields=['filed_date'])
                batch_write_filed_dates_for_hawbs(affected)
        except Exception:
            logger.exception('filed_date sync writeback failed')


def _writeback_hawbs(hawbs: list[HouseWaybill]) -> None:
    """Batch-writeback (decl + filed_date + ed_status) для списка HAWB.

    Если signals_suppressed() — пропускаем (бэдчевая операция типа reparse
    отвечает за writeback сама в конце через resync_* команды).
    Для экспортных HAWB дополнительно гарантирует строку в export-вкладке
    и пишет «Статус ЭД».
    """
    if not hawbs:
        return
    try:
        from cargo.services.sheets.writeback import (
            batch_write_declarations_for_hawbs,
            batch_write_filed_dates_for_hawbs,
            batch_write_ed_status_for_hawbs,
            ensure_export_rows_for_hawbs,
            signals_suppressed,
            _kind_for_hawb,
        )
        if signals_suppressed():
            return
        for h in hawbs:
            h.refresh_from_db(fields=['customs_declaration_number', 'filed_date'])
        # Для экспортных HAWB убедимся что строка в Sheets уже есть.
        exp = [h for h in hawbs if _kind_for_hawb(h) == 'export']
        if exp:
            ensure_export_rows_for_hawbs(exp)
        batch_write_declarations_for_hawbs(hawbs)
        batch_write_filed_dates_for_hawbs(hawbs)
        batch_write_ed_status_for_hawbs(hawbs)
        # Параллельно — realtime в CRM-вкладки специалистов (decl, status,
        # запросы). Без этого CRM узнавал только через cron каждые 5 мин,
        # что давало 5-15 мин лаг или больше (на 429-волнах от Sheets API).
        try:
            from cargo.services.sheets.crm_realtime import (
                batch_write_all_for_crm_hawbs,
            )
            batch_write_all_for_crm_hawbs(hawbs)
        except Exception:
            logger.exception('crm_realtime writeback failed (non-fatal)')
    except Exception:
        logger.exception('sheets writeback after declaration write failed')


def apply_status(msg: AltaInboxMessage,
                 cargo: Optional[Cargo],
                 hawb: Optional[HouseWaybill]) -> Optional[str]:
    """Применяет customs_declaration_number и статус.

    ДТ-номер пишется через `recompute_declaration()` — он берёт самое свежее
    по prepared_at сообщение released/withdrawn для этой пары (cargo, hawb).
    Это снимает зависимость от порядка обработки и поддерживает повторные
    подачи + отзыв декларации.

    Для release-сообщений где одна ДТ покрывает несколько HAWB партии
    (multi-waybill release) — recompute проходит по ВСЕМ HAWB этой Cargo,
    т.к. raw_xml сообщения содержит их номера, и recompute найдёт его через
    raw_xml__icontains.

    customs_status (FILED/RELEASED/REJECTED/...) выставляется по этому
    конкретному сообщению через HouseWaybill.change_customs_status().

    customs_declaration_number пишется прямым UPDATE минуя save() — иначе
    HouseWaybill.save() автостирает поле при отсутствии MAWB / неполном
    чек-листе документов.
    """
    kind = msg.msg_kind

    # 1+2. Recompute: для matched HAWB + для siblings (multi-waybill).
    # Собираем ВСЕ обновлённые HAWB в один список, делаем единый batch-writeback
    # в конце — иначе 49 sibling × per-HAWB writeback = 100+ API reads → 429.
    all_updated: list[HouseWaybill] = []
    all_updated.extend(recompute_declaration(cargo, hawb))

    if cargo and kind in ('released', 'withdrawn'):
        siblings = cargo.hawbs.all()
        if hawb:
            siblings = siblings.exclude(pk=hawb.pk)
        for sib in siblings:
            all_updated.extend(recompute_declaration(cargo, sib))

    _writeback_hawbs(all_updated)

    # Withdrawn — статус HAWB не меняем (партия не выпущена)
    if kind == 'withdrawn':
        return None

    new_status = STATUS_FROM_KIND.get(kind)
    if not new_status:
        return None  # info / withdrawn — статус не трогаем

    targets: list[HouseWaybill] = []
    if hawb:
        targets = [hawb]
    elif cargo:
        targets = list(
            cargo.hawbs
            .filter(logistics_status__in=('EXPORT_CUSTOMS', 'IMPORT_CUSTOMS'))
        )
        if not targets:
            return f'В партии {cargo.awb_number} нет HAWB в таможне'

    # Multi-waybill release: одна ДТ покрывает несколько HAWB партии,
    # но решения по разным HAWB в рамках ОДНОЙ ECD таможня может выносить
    # в РАЗНЫЕ моменты разными CMN.11350. Поэтому расширяем targets ТОЛЬКО
    # теми HAWB партии, которые упомянуты в raw_xml ИМЕННО ЭТОГО сообщения —
    # их prepared_at = реальный момент решения по ним. HAWB из той же ДТ,
    # но не упомянутые здесь, обработаются СВОИМ CMN со своим prepared_at.
    if cargo and kind in ('released', 'rejected', 'examination', 'hold'):
        decl = ''
        if targets:
            decl = (targets[0].customs_declaration_number or '').strip()
        if decl:
            existing_ids = {h.pk for h in targets}
            raw = (msg.raw_xml or '')
            candidates = cargo.hawbs.filter(
                customs_declaration_number=decl,
            ).exclude(pk__in=existing_ids)
            extra = [h for h in candidates
                     if h.hawb_number and h.hawb_number in raw]
            if extra:
                targets.extend(extra)

    # Pre-customs logistics states: HAWB ещё не дошёл до таможни в нашей логике.
    # Если CMN-выпуск приходит на такой HAWB — авто-бампим в IMPORT/EXPORT_CUSTOMS
    # перед change_customs_status, чтобы тот авто-перевёл в READY_DELIVERY
    # (импорт) или IN_TRANSIT_EXP (экспорт). Post-customs состояния
    # (READY_DELIVERY и далее) и нештатные (RETURNED/LOST) не трогаем.
    PRE_CUSTOMS = (
        'CREATED', 'TO_ORIGIN_WH', 'AT_ORIGIN_WH', 'CONSOLIDATED', 'READY_TO_SHIP',
        'IN_TRANSIT_EXP', 'ARRIVED_DEST', 'AT_SVH',
    )

    errors = []
    applied_hawbs: list[HouseWaybill] = []

    # Подавляем per-HAWB сигналы writeback (filed_date, release_date,
    # customs_declaration_number) — каждый save() в change_customs_status
    # обычно стартует фоновый поток с 2-3 API reads. На 49 HAWB одной
    # декларации = 100+ reads → 429. После цикла делаем ОДИН batch-writeback.
    #
    # Если уже внутри bulk-режима (reparse, import) — caller сам сделает resync,
    # не запускаем свой batch_write.
    from cargo.services.sheets.writeback import (
        begin_batch_writeback, end_batch_writeback,
        signals_suppressed,
        batch_write_release_dates_for_hawbs,
        batch_write_filed_dates_for_hawbs,
        batch_write_declarations_for_hawbs,
    )
    in_bulk = signals_suppressed()
    if not in_bulk:
        begin_batch_writeback()
    try:
        for h in targets:
            # refresh: recompute_declaration выше писал customs_declaration_number
            # прямым UPDATE минуя save(). Без refresh in-memory отстаёт и
            # h.change_customs_status → self.save() перетёр бы новый номер старым.
            h.refresh_from_db(fields=['customs_declaration_number', 'filed_date'])

            # Защита от downgrade финального статуса при пере-обработке
            # старых сообщений (sweeper'ы): если уже есть более свежее
            # released/rejected/withdrawn сообщение для этой HAWB — не
            # применяем статус из текущего (он только историческая запись).
            newer_final = AltaInboxMessage.objects.filter(
                hawb=h,
                prepared_at__gt=msg.prepared_at,
                msg_kind__in=('released', 'rejected', 'withdrawn'),
            ).exists()
            if newer_final:
                continue

            # CMN от таможни — это факт. Не отказываем по причине «HAWB ещё не
            # в IMPORT_CUSTOMS в нашей БД» — декларация может подаваться через
            # Альту, минуя CargoTrack-workflow.
            if new_status == 'RELEASED' and h.logistics_status in PRE_CUSTOMS:
                is_export = (h.shipment_type or 'IMPORT').upper() == 'EXPORT'
                h.logistics_status = 'EXPORT_CUSTOMS' if is_export else 'IMPORT_CUSTOMS'
                h.logistics_status_date = timezone.now()
            try:
                # msg.prepared_at = PreparationDateTime реального CMN-ответа.
                # Передаём как event_dt чтобы release_date/filed_date были
                # одинаковыми у всех HAWB одной декларации, а не разными
                # timezone.now() (= момент вызова, не момент выпуска).
                err = h.change_customs_status(new_status, user=None,
                                              event_dt=msg.prepared_at)
                if err:
                    errors.append(f'HAWB {h.hawb_number}: {err}')
                else:
                    applied_hawbs.append(h)
                    # Синхронизируем attempt для CURRENT ДТ с новым статусом.
                    cur_decl = (h.customs_declaration_number or '').strip()
                    if cur_decl and new_status in ('RELEASED', 'REJECTED'):
                        try:
                            _register_attempt(
                                h, cur_decl, status=new_status,
                                release_date=(msg.prepared_at
                                              if new_status == 'RELEASED'
                                              else None),
                                rejected_date=(msg.prepared_at
                                               if new_status == 'REJECTED'
                                               else None),
                                trigger_writeback=False)
                        except Exception:
                            logger.exception('attempt sync failed for HAWB %s',
                                             h.pk)
                    # REJECTED: стираем рег.номер и дату подачи у HAWB
                    # (история остаётся в HawbDeclarationAttempt). Юзер хочет
                    # видеть пустые поля в Sheets когда таможня отказала.
                    if new_status == 'REJECTED':
                        HouseWaybill.objects.filter(pk=h.pk).update(
                            customs_declaration_number='',
                            filed_date=None)
                        h.refresh_from_db(fields=[
                            'customs_declaration_number', 'filed_date'])
            except Exception as e:
                logger.exception('change_customs_status failed for HAWB %s', h.pk)
                errors.append(f'HAWB {h.hawb_number}: {e}')
    finally:
        if not in_bulk:
            end_batch_writeback()

    # Batch-writeback в Sheets для всех успешно изменённых HAWB.
    # Если bulk-режим (reparse) — пропускаем, caller сделает resync в конце.
    if applied_hawbs and not in_bulk:
        # refresh — change_customs_status делал save() с auto-clear правилами
        # (Rule 4 может стереть decl_number и т.п.), нужны актуальные значения.
        for h in applied_hawbs:
            h.refresh_from_db(fields=['customs_declaration_number',
                                      'filed_date', 'release_date',
                                      'customs_status', 'logistics_status'])

        def _bg_batch():
            try:
                if new_status == 'RELEASED':
                    batch_write_release_dates_for_hawbs(applied_hawbs)
                if new_status == 'FILED':
                    batch_write_filed_dates_for_hawbs(applied_hawbs)
                # decl уже записан выше в _writeback_hawbs (recompute path).
                # Но если status RELEASED стёр decl через Rule 4 — следующий
                # recompute восстановит. Здесь не дублируем.
                # ed_status — пересчитаем и запишем для экспортных HAWB.
                from cargo.services.sheets.writeback import (
                    batch_write_ed_status_for_hawbs,
                )
                batch_write_ed_status_for_hawbs(applied_hawbs)
                # Realtime CRM-writeback — параллельно с «Общее».
                try:
                    from cargo.services.sheets.crm_realtime import (
                        batch_write_all_for_crm_hawbs,
                    )
                    batch_write_all_for_crm_hawbs(applied_hawbs)
                except Exception:
                    logger.exception('crm_realtime writeback failed (non-fatal)')
            except Exception:
                logger.exception('batch writeback after apply_status failed')
        threading.Thread(target=_bg_batch, daemon=True).start()

    if not applied_hawbs and errors:
        return '; '.join(errors)
    return None


def _parse_iso_dt(s: str):
    """ISO '2026-05-19T11:26:23+03:00' → aware datetime, None если не парсится."""
    if not s:
        return None
    try:
        from django.utils.dateparse import parse_datetime
        dt = parse_datetime(s)
        if dt is None:
            return None
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        return dt
    except Exception:
        return None


def apply_consignment_decisions(msg: AltaInboxMessage,
                                cargo: Optional[Cargo]) -> Optional[str]:
    """CMN.11350: применяем решение ИЗ КАЖДОГО блока Consignment ТОЛЬКО к
    его собственным HAWB. DecisionDate блока = реальное время решения.

    В одном CMN.11350 может быть N блоков с РАЗНЫМИ решениями (HAWB-A,B —
    выпуск, HAWB-C — отказ, HAWB-D — запрос документов). Нельзя обобщать
    msg-level kind на все упомянутые HAWB — мы должны идти per-Consignment
    и применять конкретное решение к конкретным накладным с конкретной датой.

    Возвращает строку ошибок (если были) или None.
    """
    consignments = (msg.parsed_meta or {}).get('consignments') or []
    if not consignments or not cargo:
        return None

    from cargo.services.sheets.writeback import (
        begin_batch_writeback, end_batch_writeback,
        signals_suppressed,
    )

    PRE_CUSTOMS = (
        'CREATED', 'TO_ORIGIN_WH', 'AT_ORIGIN_WH', 'CONSOLIDATED', 'READY_TO_SHIP',
        'IN_TRANSIT_EXP', 'ARRIVED_DEST', 'AT_SVH',
    )

    in_bulk = signals_suppressed()
    if not in_bulk:
        begin_batch_writeback()

    errors: list[str] = []
    applied_hawbs: list[HouseWaybill] = []

    try:
        for cons in consignments:
            kind = DECISION_CODE_KIND.get(
                (cons.get('decision_code') or '').strip(), 'info')
            event_dt = (_parse_iso_dt(cons.get('decision_date') or '')
                        or msg.prepared_at)

            for hawb_num in cons.get('waybills') or []:
                h = cargo.hawbs.filter(hawb_number__iexact=hawb_num).first()
                if not h:
                    # HAWB упомянута в CMN но её нет в нашей партии — норм.
                    # Можно залогировать но не считать ошибкой.
                    continue

                # decl_number + filed_date: пишем как только в любом значимом
                # сообщении (registered/released/hold/examination/withdrawn)
                # появляется GTDNumber — юзер хочет видеть рег.номер сразу
                # как его присвоит таможня, не дожидаясь выпуска.
                # refresh_from_db: recompute пишет в DB через UPDATE минуя
                # save() — in-memory h.customs_declaration_number отстаёт.
                if kind in ('released', 'withdrawn', 'registered',
                            'hold', 'examination', 'rejected'):
                    recompute_declaration(cargo, h)
                    h.refresh_from_db(fields=[
                        'customs_declaration_number', 'filed_date',
                    ])

                # Защита от downgrade финального статуса (sweeper'ы):
                # если есть более свежее released/rejected/withdrawn для
                # этой HAWB — не меняем status (только recompute decl).
                newer_final = AltaInboxMessage.objects.filter(
                    hawb=h,
                    prepared_at__gt=msg.prepared_at,
                    msg_kind__in=('released', 'rejected', 'withdrawn'),
                ).exists()
                if newer_final:
                    continue

                new_status = STATUS_FROM_KIND.get(kind)
                if not new_status:
                    continue  # info, withdrawn — статус не меняем

                # Авто-бамп pre-customs в IMPORT/EXPORT_CUSTOMS перед выпуском
                if new_status == 'RELEASED' and h.logistics_status in PRE_CUSTOMS:
                    is_export = (h.shipment_type or 'IMPORT').upper() == 'EXPORT'
                    h.logistics_status = 'EXPORT_CUSTOMS' if is_export else 'IMPORT_CUSTOMS'
                    h.logistics_status_date = timezone.now()

                try:
                    err = h.change_customs_status(new_status, user=None,
                                                  event_dt=event_dt)
                    if err:
                        errors.append(f'HAWB {h.hawb_number}: {err}')
                    else:
                        applied_hawbs.append(h)
                        # Синхронизируем attempt для текущей ДТ.
                        cur_decl = (h.customs_declaration_number or '').strip()
                        if cur_decl and new_status in ('RELEASED', 'REJECTED'):
                            try:
                                _register_attempt(
                                    h, cur_decl, status=new_status,
                                    release_date=(event_dt
                                                  if new_status == 'RELEASED'
                                                  else None),
                                    rejected_date=(event_dt
                                                   if new_status == 'REJECTED'
                                                   else None),
                                    trigger_writeback=False)
                            except Exception:
                                logger.exception(
                                    'attempt sync (consignment) failed '
                                    'for HAWB %s', h.pk)
                        # REJECTED: стираем decl + filed_date у HAWB,
                        # история остаётся в HawbDeclarationAttempt.
                        if new_status == 'REJECTED':
                            HouseWaybill.objects.filter(pk=h.pk).update(
                                customs_declaration_number='',
                                filed_date=None)
                            h.refresh_from_db(fields=[
                                'customs_declaration_number', 'filed_date'])
                except Exception as e:
                    logger.exception('change_customs_status failed for HAWB %s', h.pk)
                    errors.append(f'HAWB {h.hawb_number}: {e}')
    finally:
        if not in_bulk:
            end_batch_writeback()

    # Финальный batch-writeback в Sheets для всех HAWB которые
    # реально получили новый статус. Без этого multi-HAWB CMN.11350
    # (10 consignments в одном сообщении) меняет DB но Sheets не
    # обновляется — сигналы подавлены, явный вызов batch_write нужен.
    if applied_hawbs and not in_bulk:
        for h in applied_hawbs:
            h.refresh_from_db(fields=[
                'customs_declaration_number', 'filed_date',
                'release_date', 'customs_status', 'logistics_status',
            ])

        def _bg_batch():
            try:
                from cargo.services.sheets.writeback import (
                    batch_write_release_dates_for_hawbs,
                    batch_write_filed_dates_for_hawbs,
                    batch_write_declarations_for_hawbs,
                    batch_write_ed_status_for_hawbs,
                    batch_write_attempts_count_for_hawbs,
                )
                batch_write_release_dates_for_hawbs(applied_hawbs)
                batch_write_filed_dates_for_hawbs(applied_hawbs)
                batch_write_declarations_for_hawbs(applied_hawbs)
                batch_write_attempts_count_for_hawbs(applied_hawbs)
                batch_write_ed_status_for_hawbs(applied_hawbs)
                # Realtime CRM-writeback — параллельно с «Общее».
                try:
                    from cargo.services.sheets.crm_realtime import (
                        batch_write_all_for_crm_hawbs,
                    )
                    batch_write_all_for_crm_hawbs(applied_hawbs)
                except Exception:
                    logger.exception('crm_realtime (consignment) failed')
            except Exception:
                logger.exception('consignment final writeback failed')
        import threading as _th
        _th.Thread(target=_bg_batch, daemon=True).start()

    return '; '.join(errors) if errors else None


def emit_event(msg: AltaInboxMessage,
               cargo: Optional[Cargo],
               hawb: Optional[HouseWaybill]) -> None:
    """Создаёт HawbWorkflowEvent в таймлайне HAWB(ов)."""
    event_type = EVENT_TYPE_FROM_KIND.get(msg.msg_kind, 'OTHER')
    occurred = msg.prepared_at or msg.received_at or timezone.now()

    hawbs: list[HouseWaybill] = []
    if hawb:
        hawbs = [hawb]
    elif cargo:
        hawbs = list(cargo.hawbs.all())

    for h in hawbs:
        HawbWorkflowEvent.objects.update_or_create(
            hawb=h,
            event_type=event_type,
            source_row=None,
            defaults={
                'occurred_at': occurred,
                'raw_value': msg.declaration_number or msg.msg_type,
                'comment': msg.get_msg_kind_display(),
                'source': 'alta',
            },
        )


def trigger_sheets_writeback(hawb: HouseWaybill) -> None:
    """Лёгкий фон. Не блокирует ответ агенту, не валится в основной flow."""
    def _run():
        try:
            from cargo.services.sheets.writeback import write_declaration  # noqa
            write_declaration(hawb)
        except ImportError:
            # writeback модуль ещё не реализован — нормальный no-op для этой итерации
            logger.info('sheets writeback module not available yet, skipping')
        except Exception:
            logger.exception('sheets writeback failed for HAWB %s', hawb.pk)
    threading.Thread(target=_run, daemon=True).start()


def match_svh(msg: AltaInboxMessage) -> Optional[Cargo]:
    """Подбирает Cargo для СВХ-сообщения через MAWB из parsed_meta.

    Альта пишет MAWB с разделителем-точкой (`222-.40333075`), наш Cargo
    хранит его без точки (`222-40333075`). Нормализация уже сделана в
    парсере — берём `parsed_meta['svh_mawb']`.
    """
    parsed = msg.parsed_meta or {}
    mawb = (parsed.get('svh_mawb') or '').strip()
    if not mawb:
        return None
    return Cargo.objects.filter(awb_number__iexact=mawb).first()


def match_svh_do1(msg: AltaInboxMessage) -> tuple[Optional[Cargo], Optional[AltaInboxMessage]]:
    """Match CMN.13010 (регистрация ДО1) → Cargo.

    Полная цепочка операции (по описанию пользователя 2026-05-26):
      1. В Альта-ГТД формируем представление с MAWB →
      2. CMN.13029 уходит в таможню → таможня регистрирует, возвращает
         регистрационный номер →
      3. В Альта-СВХ оператор регистрирует груз отдельной операцией,
         вписывает этот рег.номер CMN.13029 →
      4. Из регистрации груза формируется ED.DO1 (наше исходящее в
         Альта-СВХ) с MAWB в CommonWayBillNumber и нашим report_number →
      5. Альта-СВХ отправляет ED.DO1 в таможню, таможня регистрирует ДО1
         и присылает ответ CMN.13010, где в DO1KeepLimits/DO1ReportLinkData
         лежит ReportNumber — это наш report_number из ED.DO1.

    Жёсткий якорь (с 2026-05-26): CMN.13010.DO1ReportLinkData.ReportNumber ==
    AltaOutboxObservation(msg_type=ED.DO1).parsed_meta['report_number'].
    Наш ED.DO1 уже привязан к Cargo через common_waybill_number (MAWB).

    Fallback (time-эвристика, для старых сообщений до 2026-05-26 или если в
    XML нет DO1ReportLinkData): для нашей лицензии цепочка
        представление (CMN.13029)
            ↓ часы / иногда сутки
        регистрация груза (внутри Альта-СВХ)
            ↓
        ДО1 (CMN.13010)
    Представление всегда строго раньше ДО1. Берём ближайшее представление
    в окне `LOOKBACK` (7 дней) у которого Cargo ещё без svh_do1_reg_number.

    Возвращает (cargo, опционально-представление).
    """
    from datetime import timedelta

    pm = msg.parsed_meta or {}
    link_report = (pm.get('do1_link_report_number') or '').strip()
    if link_report:
        # Точный якорь: ED.DO1 с этим report_number → его Cargo.
        from cargo.models import AltaOutboxObservation
        outbox = (
            AltaOutboxObservation.objects
            .filter(msg_type='ED.DO1')
            .filter(parsed_meta__report_number=link_report)
            .exclude(cargo=None)
            .select_related('cargo')
            .order_by('-prepared_at')
            .first()
        )
        if outbox and outbox.cargo:
            return (outbox.cargo, None)

    # Fallback на time-эвристику
    if not msg.prepared_at:
        return (None, None)

    LOOKBACK = timedelta(days=7)
    window_start = msg.prepared_at - LOOKBACK

    presentations = (
        AltaInboxMessage.objects
        .filter(
            msg_type='CMN.13029',
            msg_kind='svh_placed',
            prepared_at__gte=window_start,
            prepared_at__lte=msg.prepared_at,
        )
        .exclude(cargo=None)
        .exclude(cargo__svh_do1_reg_number__gt='')
        .select_related('cargo')
        .order_by('-prepared_at')
    )
    nearest = presentations.first()
    if nearest:
        return (nearest.cargo, nearest)
    return (None, None)


def _writeback_svh_cargo(cargo: Cargo) -> None:
    """Лёгкий фон. Триггерит запись лицензии СВХ и даты размещения в Sheets.

    Сообщение СВХ привязано к Cargo (партии целиком), а строки Sheets
    идут по HAWB-ам. Writeback итерирует по всем HAWB партии — для каждого
    проставляет общую дату/лицензию в две новые колонки.
    """
    def _run():
        try:
            from cargo.services.sheets.writeback import write_svh_placement_for_cargo
            write_svh_placement_for_cargo(cargo)
        except ImportError:
            logger.info('svh writeback not available yet, skipping')
        except Exception:
            logger.exception('svh writeback failed for cargo %s', cargo.pk)
    threading.Thread(target=_run, daemon=True).start()


def apply_svh_placement(msg: AltaInboxMessage, cargo: Cargo) -> Optional[str]:
    """Обработка представления (CMN.13029).

    С 2026-05-27: пишет лицензию СВХ в Cargo сразу. Раньше ждали CMN.13010
    («партия размещена»), но юзер указал на кейс «выпуск с колёс» — груз
    идёт через ECD без фактического СВХ-хранения, ДО1 не оформляется,
    CMN.13010 никогда не прилетит, и Cargo до бесконечности оставался без
    лицензии. Лицензия из CMN.13029 фактически достоверная, поэтому
    проставляем её сразу. Остальные СВХ-поля (svh_do1_reg_number,
    scan_into_bond) по-прежнему ставит только apply_svh_do1.

    Также триггерит backfill: если CMN.13010 для этой партии уже пришла
    раньше представления (race), сейчас доматчит его.
    """
    parsed = msg.parsed_meta or {}
    lic = (parsed.get('svh_warehouse_license') or '').strip()
    if lic and not (cargo.warehouse_license or '').strip():
        Cargo.objects.filter(pk=cargo.pk).update(warehouse_license=lic)
        cargo.warehouse_license = lic
        logger.info('apply_svh_placement: cargo %s warehouse_license=%s '
                    'set from CMN.13029', cargo.pk, lic)
        _writeback_svh_cargo(cargo)

    _backfill_do1_for_presentation(msg, cargo)
    return None


def _backfill_do1_for_presentation(presentation_msg: AltaInboxMessage,
                                   cargo: Cargo) -> None:
    """Если CMN.13010 пришла, но к моменту не было представления —
    подхватываем «зависшие» ДО1 (без cargo) после привязки представления.

    Так как UUID-якоря нет, используем то же окно по времени что и
    match_svh_do1, но в обратную сторону: ДО1 в окне `[prepared_at,
    prepared_at + 4 часа]` после представления = вероятно той же
    операции (см. комментарий match_svh_do1).
    """
    from datetime import timedelta

    if not presentation_msg.prepared_at:
        return

    LOOKAHEAD = timedelta(hours=4)
    window_end = presentation_msg.prepared_at + LOOKAHEAD

    pending = AltaInboxMessage.objects.filter(
        msg_kind='svh_do1_registered',
        cargo__isnull=True,
        prepared_at__gte=presentation_msg.prepared_at,
        prepared_at__lte=window_end,
    ).order_by('prepared_at')
    for do1 in pending:
        # Защита: если в окне есть БОЛЕЕ ранее представление чем наше —
        # тот ДО1 матчится туда, не сюда.
        ahead = AltaInboxMessage.objects.filter(
            msg_type='CMN.13029',
            msg_kind='svh_placed',
            prepared_at__gt=presentation_msg.prepared_at,
            prepared_at__lte=do1.prepared_at,
        ).exists()
        if ahead:
            continue
        do1.cargo = cargo
        do1.save(update_fields=['cargo'])
        apply_svh_do1(do1, cargo)


def apply_svh_do1(msg: AltaInboxMessage, cargo: Cargo) -> Optional[str]:
    """Обработка регистрации ДО1 (CMN.13010).

    Заполняет Cargo:
    - svh_do1_reg_number ← рег.номер ДО1 (например 10001020/230526/5012272)
    - scan_into_bond ← дата+время регистрации ДО1
    - warehouse_license ← если ещё пусто

    Перезаписывает предыдущие значения (например, если представление
    раньше проставило неверные данные). Триггерит writeback.
    """
    from datetime import datetime, time as dt_time
    from django.utils.dateparse import parse_date
    from django.utils import timezone as tz

    parsed = msg.parsed_meta or {}
    license_ = (parsed.get('svh_warehouse_license') or '').strip()
    do1_date = (parsed.get('svh_do1_reg_date') or '').strip()
    do1_time = (parsed.get('svh_do1_reg_time') or '').strip()
    do1_reg  = (parsed.get('svh_do1_reg_number') or '').strip()

    update_fields = []
    if license_ and not (cargo.warehouse_license or '').strip():
        cargo.warehouse_license = license_
        update_fields.append('warehouse_license')

    if do1_reg and (cargo.svh_do1_reg_number or '').strip() != do1_reg:
        cargo.svh_do1_reg_number = do1_reg
        update_fields.append('svh_do1_reg_number')

    if do1_date:
        d = parse_date(do1_date)
        if d:
            t = dt_time(0, 0)
            if do1_time:
                try:
                    h, m, s = do1_time.split(':', 2)
                    sec = int(float(s.split('.')[0]))
                    t = dt_time(int(h), int(m), sec)
                except (ValueError, IndexError):
                    pass
            new_dt = tz.make_aware(datetime.combine(d, t))
            # Перезаписываем scan_into_bond — он мог быть выставлен по
            # представлению (была дата представления, не ДО1). Реальная
            # дата размещения — момент регистрации ДО1.
            if cargo.scan_into_bond != new_dt:
                cargo.scan_into_bond = new_dt
                update_fields.append('scan_into_bond')

    if update_fields:
        cargo.save(update_fields=update_fields)

    _writeback_svh_cargo(cargo)
    return None


def match_svh_do2(msg: AltaInboxMessage) -> tuple[Optional[Cargo], list[HouseWaybill]]:
    """Подбирает Cargo и конкретные HAWB для CMN.13014 (ДО2 — выпуск со СВХ).

    Один CMN.13014 = одно событие выпуска со склада. Может покрывать одну
    или несколько HAWB партии (зависит от того сколько перечислено в
    TransportDoc / ProduceDocuments). НЕЛЬЗЯ растягивать ДО2 на всю партию —
    в одной партии бывает много ДО2 на разные ДТ/HAWB в разное время.

    Стратегия — ТОЛЬКО прямой матчинг HAWB-номера из TransportDoc:
    1. По svh_do2_doc_numbers ищем Cargo (по MAWB) → потом HAWB партии
       чьи hawb_number есть в этом же списке.

    Намеренно НЕ матчим через customs_declaration_number из ProduceDocuments:
    при multi-waybill релизе одна ДТ покрывает несколько HAWB партии, но
    ДО2 относится только к КОНКРЕТНЫМ HAWB перечисленным в TransportDoc.
    Матчинг по ДТ растянул бы дату ДО2 одной HAWB на всех её siblings.

    Возвращает (cargo, [hawbs]). [hawbs] может быть пустым — тогда
    apply_svh_do2 НЕ применяет ничего (не растягиваем на всю партию).
    """
    parsed = msg.parsed_meta or {}

    doc_numbers_raw = parsed.get('svh_do2_doc_numbers') or []
    doc_numbers = [
        (n or '').replace('.', '').replace(' ', '').strip()
        for n in doc_numbers_raw
    ]
    doc_numbers = [n for n in doc_numbers if n]

    # Поиск Cargo: по MAWB в TransportDoc → по ДО-1 fallback
    cargo: Optional[Cargo] = None
    for num in doc_numbers:
        cargo = Cargo.objects.filter(awb_number__iexact=num).first()
        if cargo:
            break
    if not cargo:
        do1_ref = (parsed.get('svh_do2_do1_ref') or '').strip()
        if do1_ref:
            cargo = Cargo.objects.filter(svh_do1_reg_number=do1_ref).first()

    if not cargo:
        return (None, [])

    # Конкретные HAWB — только прямое упоминание hawb_number в TransportDoc
    hawbs = list(cargo.hawbs.filter(hawb_number__in=doc_numbers)) if doc_numbers else []
    return (cargo, hawbs)


def apply_svh_do2(msg: AltaInboxMessage,
                  cargo: Cargo,
                  hawbs: list[HouseWaybill]) -> Optional[str]:
    """Обработка ДО2 (CMN.13014, WHGoodOut — выпуск груза со СВХ).

    Заполняет HouseWaybill.svh_do2_send_at у каждой HAWB из hawbs.
    На уровне Cargo пишем только warehouse_license (если ещё пусто).

    Если hawbs пуст — НЕ применяем ничего, чтобы не растянуть дату на
    всю партию (см. docstring match_svh_do2).
    """
    from datetime import datetime, time as dt_time
    from django.utils.dateparse import parse_date
    from django.utils import timezone as tz

    parsed = msg.parsed_meta or {}
    license_ = (parsed.get('svh_warehouse_license') or '').strip()
    do2_date = (parsed.get('svh_do2_send_date') or '').strip()
    do2_time = (parsed.get('svh_do2_send_time') or '').strip()

    # Лицензия — на Cargo (один раз)
    if license_ and not (cargo.warehouse_license or '').strip():
        cargo.warehouse_license = license_
        cargo.save(update_fields=['warehouse_license'])

    if not hawbs:
        # Нет конкретных HAWB — нечего применять.
        return None

    # Парсим момент выпуска: SendDate + SendTime
    new_dt = None
    if do2_date:
        d = parse_date(do2_date)
        if d:
            t = dt_time(0, 0)
            if do2_time:
                try:
                    h, m, s = do2_time.split(':', 2)
                    sec = int(float(s.split('.')[0]))
                    t = dt_time(int(h), int(m), sec)
                except (ValueError, IndexError):
                    pass
            new_dt = tz.make_aware(datetime.combine(d, t))

    if not new_dt:
        return None

    # Записываем svh_do2_send_at у каждой HAWB прямым UPDATE минуя save()
    # (чтобы не дёргать save()-rules, которые могут стереть decl_number и т.п.)
    updated_pks = [h.pk for h in hawbs]
    HouseWaybill.objects.filter(pk__in=updated_pks).update(svh_do2_send_at=new_dt)

    # Триггерим per-HAWB writeback в Sheets
    refreshed = list(HouseWaybill.objects.filter(pk__in=updated_pks))
    _writeback_svh_do2_hawbs(refreshed)
    return None


def _writeback_svh_do2_hawbs(hawbs: list[HouseWaybill]) -> None:
    """Per-HAWB writeback дат ДО2 в Sheets. Под suppress — no-op."""
    if not hawbs:
        return
    try:
        from cargo.services.sheets.writeback import (
            batch_write_svh_do2_dates_for_hawbs,
            signals_suppressed,
        )
        if signals_suppressed():
            return
        batch_write_svh_do2_dates_for_hawbs(hawbs)
    except ImportError:
        logger.info('svh ДО2 writeback module not available')
    except Exception:
        logger.exception('svh ДО2 writeback failed')


def apply_customs_request(msg: AltaInboxMessage) -> None:
    """Создаёт/обновляет HawbCustomsRequest для каждого <RequestedDoc>
    в одном ED.11003 envelope (один envelope может нести N запросов).

    Привязка к HAWB:
      1) AltaOutboxObservation(envelope_id=InitialEnvelopeID) — наше
         исходящее CMN.11349;
      2) если есть raw_xml + Position → вычисляем конкретную HAWB по
         диапазонам товаров (hawb_for_position_cmn_11349);
      3) fallback: одна HAWB в подаче → её и берём.
    """
    from cargo.models import (AltaOutboxObservation, HawbCustomsRequest,
                              HouseWaybill)
    from cargo.services.alta.xml_extract import (
        hawb_for_position_cmn_11349, parse_ed_11003,
    )
    from django.utils.dateparse import parse_datetime
    from datetime import datetime as _dt

    # Парсим raw_xml целиком — нужен массив requests
    pm = dict(msg.parsed_meta or {})
    if msg.raw_xml:
        pm.update(parse_ed_11003(msg.raw_xml))
        msg.parsed_meta = pm

    requests = pm.get('requests') or []
    if not requests:
        logger.info('ED.11003 %s: no <RequestedDoc> blocks found',
                    msg.envelope_id)
        return

    # Находим наш исходящий outbox. ED.11003 не несёт InitialEnvelopeID в
    # routing-блоке, поэтому используем два резервных якоря:
    #   1) ProcessID (UUID процесса) совпадает с ProcessID в нашем
    #      CMN.11349/11023 (это надёжная связь — таможня сохраняет процесс
    #      ID через всю цепочку сообщений).
    #   2) GTDNumber в теле ED.11003 (когда таможня зарегистрировала ДТ —
    #      номер совпадает с HouseWaybill.customs_declaration_number).
    process_id_xml = ''
    if msg.raw_xml:
        m_pid = re.search(
            r'<(?:[\w-]+:)?ProccessID\b[^>]*>([^<]+)</(?:[\w-]+:)?ProccessID>',
            msg.raw_xml)
        if m_pid:
            process_id_xml = m_pid.group(1).strip()

    outbox_raw_xml = ''
    outbox_hawbs: list = []
    outbox_msg_type = ''
    outbox = None
    initial_env = ''
    if process_id_xml:
        # Поиск среди CMN.11349/11023 у которых в raw_xml есть тот же
        # ProcessID. SQLite не умеет efficient JSON-lookup, но outbox-список
        # небольшой (~сотни) — линейный обход допустим.
        for o in (AltaOutboxObservation.objects
                  .filter(msg_type__in=['CMN.11349', 'CMN.11023'])
                  .iterator()):
            raw = (o.parsed_meta or {}).get('raw_xml') or ''
            if process_id_xml in raw:
                outbox = o
                break
    if outbox:
        outbox_meta = outbox.parsed_meta or {}
        outbox_raw_xml = outbox_meta.get('raw_xml') or ''
        outbox_hawbs = outbox_meta.get('hawbs') or []
        outbox_msg_type = outbox.msg_type
        initial_env = outbox.envelope_id  # для сохранения в HawbCustomsRequest

    # Fallback-якорь: GTDNumber в теле ED.11003. Когда таможня
    # зарегистрировала декларацию, она в каждом ED.11003 пишет полный
    # GTDNumber: <CustomsCode>/<RegistrationDate>/<GTDNumber>. По нему
    # можно найти HouseWaybill.customs_declaration_number напрямую.
    decl_from_gtd = ''
    if msg.raw_xml and not outbox:
        gtd_block = re.search(
            r'<rid:GTDNumber\b[^>]*>(.*?)</rid:GTDNumber>',
            msg.raw_xml, re.S)
        if gtd_block:
            body = gtd_block.group(1)
            cc = re.search(r'>([^<]+)</(?:[\w-]+:)?CustomsCode>', body)
            rd = re.search(r'>([^<]+)</(?:[\w-]+:)?RegistrationDate>', body)
            gn = re.search(r'>([^<]+)</(?:[\w-]+:)?GTDNumber>', body)
            cc = cc.group(1).strip() if cc else ''
            rd = rd.group(1).strip() if rd else ''
            gn = gn.group(1).strip() if gn else ''
            # Формат БД: 10229030/280526/5036325 (DDMMYY компактно)
            if cc and rd and gn and rd != '0001-01-01' and gn != '000000':
                try:
                    y, m_, d = rd.split('-')
                    rd_short = f'{d}{m_}{y[2:]}'
                    decl_from_gtd = f'{cc}/{rd_short}/{gn}'
                except ValueError:
                    pass

    touched_hawbs: set = set()
    for req in requests:
        position_raw = (req.get('position') or '').strip()
        try:
            position = int(position_raw) if position_raw else None
        except ValueError:
            position = None

        hawb = None
        # 1) Через ProcessID-сматченный outbox: CMN.11349 + Position → HAWB.
        if outbox_msg_type == 'CMN.11349' and outbox_raw_xml and position:
            hn = hawb_for_position_cmn_11349(outbox_raw_xml, position)
            if hn:
                hawb = HouseWaybill.objects.filter(
                    hawb_number__iexact=hn).first()
        # 2) Outbox с одной HAWB (CMN.11023/single-HAWB CMN.11349) → её.
        if not hawb and len(outbox_hawbs) == 1:
            hawb = HouseWaybill.objects.filter(
                hawb_number__iexact=str(outbox_hawbs[0]).strip()).first()
        # 3) Outbox не нашёлся, но в теле ED.11003 есть номер ДТ → ищем
        #    HouseWaybill по customs_declaration_number. Применяется к ВСЕМ
        #    HAWB этой декларации (а не per-Position — раз outbox потерян,
        #    точную привязку не построим).
        if not hawb and decl_from_gtd:
            hawb = HouseWaybill.objects.filter(
                customs_declaration_number=decl_from_gtd).first()

        # datetime запроса
        request_dt = None
        dt_str = (req.get('request_dt_msk') or '').strip()
        if dt_str:
            request_dt = parse_datetime(dt_str)
        date_limit = None
        dl_str = (req.get('date_limit') or '').strip()
        if dl_str:
            try:
                date_limit = _dt.strptime(dl_str, '%Y-%m-%d').date()
            except ValueError:
                pass

        HawbCustomsRequest.objects.update_or_create(
            envelope_id=msg.envelope_id,
            request_position_id=(req.get('request_position_id') or ''),
            defaults={
                'initial_envelope_id': initial_env,
                'prepared_at':         msg.prepared_at,
                'request_dt_msk':      request_dt,
                'date_limit':          date_limit,
                'request_position':    position,
                'requestor_name':      req.get('requestor_name', ''),
                'customs_code':        pm.get('customs_code', ''),
                'office_name':         pm.get('office_name', ''),
                'request_text':        (req.get('request_text') or '').strip(),
                'raw_xml':             msg.raw_xml or '',
                'parsed_meta':         req,
                'hawb':                hawb,
            },
        )
        if hawb:
            touched_hawbs.add(hawb.pk)

    logger.info('ED.11003 %s applied: %d requests, %d HAWBs touched',
                msg.envelope_id, len(requests), len(touched_hawbs))

    for pk in touched_hawbs:
        h = HouseWaybill.objects.filter(pk=pk).first()
        if h:
            _writeback_customs_requests_for_hawb(h)


def _writeback_customs_requests_for_hawb(hawb: HouseWaybill) -> None:
    """Запускает writeback запросов, счётчика и ed_status в Sheets для HAWB."""
    def _run():
        try:
            from cargo.services.sheets.writeback import (
                batch_write_customs_requests_for_hawbs,
                batch_write_customs_requests_count_for_hawbs,
                batch_write_ed_status_for_hawbs,
                ensure_export_rows_for_hawbs,
                _kind_for_hawb,
            )
            if _kind_for_hawb(hawb) == 'export':
                ensure_export_rows_for_hawbs([hawb])
            batch_write_customs_requests_for_hawbs([hawb])
            batch_write_customs_requests_count_for_hawbs([hawb])
            batch_write_ed_status_for_hawbs([hawb])
            # Realtime CRM-writeback — ed_status + запросы в спец-вкладки.
            try:
                from cargo.services.sheets.crm_realtime import (
                    batch_write_all_for_crm_hawbs,
                )
                batch_write_all_for_crm_hawbs([hawb])
            except Exception:
                logger.exception(
                    'crm_realtime customs_request writeback failed')
        except ImportError:
            pass
        except Exception:
            logger.exception('customs_request writeback failed for HAWB %s',
                             hawb.hawb_number)
    threading.Thread(target=_run, daemon=True).start()


def _apply_goods_count_from_cmn11010(
    msg: AltaInboxMessage,
    cargo,
    hawb,
) -> None:
    """CMN.11010: <TotalGoodsNumber> → HouseWaybill.goods_count.

    Multi-waybill: total один на всех HAWB декларации (НЕ делим).
    Защита от cross-pollination: siblings берём только если их hawb_number
    упомянут в raw_xml этого envelope (hard-anchor правило).
    Idempotent: пропускаем HAWB где goods_count уже совпадает.
    """
    raw = msg.raw_xml or ''
    if not raw:
        return
    from cargo.services.alta.xml_extract import count_positions_cmn_11010
    total = count_positions_cmn_11010(raw)
    if not total:
        return

    targets: list = []
    if hawb:
        targets.append(hawb)
    decl = ''
    if hawb and (hawb.customs_declaration_number or '').strip():
        decl = hawb.customs_declaration_number.strip()
    if cargo and decl:
        sib_qs = cargo.hawbs.filter(customs_declaration_number=decl)
        if hawb:
            sib_qs = sib_qs.exclude(pk=hawb.pk)
        for sib in sib_qs:
            if sib.hawb_number and sib.hawb_number in raw:
                targets.append(sib)

    if not targets:
        return

    affected = []
    for h in targets:
        if h.goods_count == total:
            continue
        HouseWaybill.objects.filter(pk=h.pk).update(goods_count=total)
        affected.append(h)

    if not affected:
        return
    logger.info('CMN.11010 goods_count=%s: updated %d HAWBs (msg #%s)',
                total, len(affected), msg.pk)
    try:
        from cargo.services.sheets.writeback import (
            batch_write_goods_count_for_hawbs, signals_suppressed,
        )
        if signals_suppressed():
            return
        for h in affected:
            h.refresh_from_db(fields=['goods_count'])

        def _bg():
            try:
                batch_write_goods_count_for_hawbs(affected)
            except Exception:
                logger.exception('CMN.11010 goods_count writeback failed')
        threading.Thread(target=_bg, daemon=True).start()
    except Exception:
        logger.exception('CMN.11010 goods_count writeback dispatch failed')


def dispatch(msg: AltaInboxMessage) -> None:
    """Главная точка входа: матчинг → recompute ДТ → статус → событие."""
    msg.msg_kind = classify(msg.msg_type, msg.parsed_meta)

    # ── Запрос документов (MY.11003) ──
    if msg.msg_kind == 'customs_request':
        try:
            apply_customs_request(msg)
            msg.status_applied = True
        except Exception as e:
            logger.exception('apply_customs_request failed')
            msg.parsed_meta = {**(msg.parsed_meta or {}), 'apply_error': str(e)}
            msg.status_applied = False
        msg.save(update_fields=['msg_kind', 'parsed_meta',
                                'status_applied'])
        return

    # ── СВХ-ветка: представление (CMN.13029) ──
    if msg.msg_kind == 'svh_placed':
        cargo = match_svh(msg)
        if cargo:
            msg.cargo = cargo
            msg.save(update_fields=['msg_kind', 'cargo', 'parsed_meta'])
            err = apply_svh_placement(msg, cargo)
            if err:
                msg.parsed_meta = {**(msg.parsed_meta or {}), 'apply_error': err}
                msg.status_applied = False
            else:
                msg.status_applied = True
            msg.save(update_fields=['status_applied', 'parsed_meta'])
        else:
            msg.save(update_fields=['msg_kind', 'cargo', 'hawb',
                                    'status_applied', 'parsed_meta'])
        return

    # ── СВХ-ветка: регистрация ДО1 (CMN.13010) ──
    if msg.msg_kind == 'svh_do1_registered':
        cargo, presentation = match_svh_do1(msg)
        if cargo:
            msg.cargo = cargo
            msg.save(update_fields=['msg_kind', 'cargo', 'parsed_meta'])
            err = apply_svh_do1(msg, cargo)
            if err:
                msg.parsed_meta = {**(msg.parsed_meta or {}), 'apply_error': err}
                msg.status_applied = False
            else:
                msg.status_applied = True
            msg.save(update_fields=['status_applied', 'parsed_meta'])
        else:
            # Представление ещё не пришло (race) — оставляем висеть.
            # Когда представление прибудет → _backfill_do1_for_presentation
            # подхватит это сообщение.
            msg.save(update_fields=['msg_kind', 'cargo', 'hawb',
                                    'status_applied', 'parsed_meta'])
        return

    # ── СВХ-ветка: ДО2, выпуск со склада (CMN.13014) ──
    if msg.msg_kind == 'svh_do2_registered':
        cargo, do2_hawbs = match_svh_do2(msg)
        if cargo:
            msg.cargo = cargo
            # Если найдена одна конкретная HAWB — фиксируем её на msg
            # для удобства фильтрации в админке/UI.
            if len(do2_hawbs) == 1:
                msg.hawb = do2_hawbs[0]
            msg.save(update_fields=['msg_kind', 'cargo', 'hawb', 'parsed_meta'])
            err = apply_svh_do2(msg, cargo, do2_hawbs)
            if err:
                msg.parsed_meta = {**(msg.parsed_meta or {}), 'apply_error': err}
                msg.status_applied = False
            else:
                msg.status_applied = True
            msg.save(update_fields=['status_applied', 'parsed_meta'])
        else:
            msg.save(update_fields=['msg_kind', 'cargo', 'hawb',
                                    'status_applied', 'parsed_meta'])
        return

    # ── ED-таможня (existing flow) ──
    cargo, hawb = match(msg)
    if cargo or hawb:
        msg.cargo = cargo
        msg.hawb = hawb
        # Сохраняем привязки ДО apply_*: recompute_declaration читает
        # AltaInboxMessage.objects.filter(...) и должен увидеть и это сообщение.
        msg.save(update_fields=['msg_kind', 'cargo', 'hawb', 'parsed_meta'])

        # CMN.11350 (ExpressCargoDeclarationCustomMark) — per-HAWB решения
        # в блоках <Consignment>. Идём блок за блоком, не обобщая на всех
        # упомянутых siblings одно msg.kind.
        consignments = (msg.parsed_meta or {}).get('consignments') or []
        if consignments and cargo:
            err = apply_consignment_decisions(msg, cargo)
        else:
            err = apply_status(msg, cargo, hawb)
        if err:
            msg.parsed_meta = {**(msg.parsed_meta or {}), 'apply_error': err}
            msg.status_applied = False
        else:
            msg.status_applied = True
            emit_event(msg, cargo, hawb)
            # CMN.11010: проставляем HouseWaybill.goods_count из
            # TotalGoodsNumber. Узкий тип — 11309/11350 имеют другую
            # XML-структуру и сюда не попадают.
            if msg.msg_type == 'CMN.11010':
                try:
                    _apply_goods_count_from_cmn11010(msg, cargo, hawb)
                except Exception:
                    logger.exception(
                        'CMN.11010 goods_count apply failed for msg #%s',
                        msg.pk,
                    )
        msg.save(update_fields=['status_applied', 'parsed_meta'])
    else:
        msg.save(update_fields=['msg_kind', 'cargo', 'hawb',
                                'status_applied', 'parsed_meta'])
