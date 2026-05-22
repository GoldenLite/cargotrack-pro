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
import threading
from typing import Optional

from django.utils import timezone

from cargo.models import AltaInboxMessage, HawbWorkflowEvent, HouseWaybill


logger = logging.getLogger('cargo.alta.inbox')


# ─── маппинг MessageType на наш semantic kind ──
# Из реальных .gz из C:\GTDSERV\ED\IN.
MSG_KIND_MAP: dict[str, str] = {
    'CMN.00003': 'info',         # ArchResult — ACK от gateway: «обработано»
    'CMN.11010': 'released',     # ED_Container «Выпуск товаров разрешен» (DecisionCode 10)
    'CMN.11350': 'released',     # ExpressCargoDeclarationCustomMark — отметка таможни.
                                 # DecisionCode 10=выпуск, 90=отказ. Уточняется в classify_with_body().
    'CMN.11314': 'info',         # Закрытие процедуры (DO1Close)
    'CMN.13021': 'info',         # DO1KeepLimits — лимит хранения / размещение на СВХ
}

# DecisionCode → конкретный kind для типов где он есть в теле.
# 10 — выпуск, 90 — отказ; остальные считаем info до выяснения.
DECISION_CODE_KIND: dict[str, str] = {
    '10': 'released',
    '11': 'released',
    '90': 'rejected',
    '91': 'rejected',
}


def classify(msg_type: str, parsed_meta: Optional[dict] = None) -> str:
    """MessageType (+ опц parsed_meta из тела) → kind.

    Если у MessageType есть DecisionCode в теле — уточняем kind по нему.
    Неизвестные коды → 'info' (статус не меняем).
    """
    base = MSG_KIND_MAP.get((msg_type or '').strip(), 'info')
    if parsed_meta:
        dc = (parsed_meta.get('decision_code') or '').strip()
        if dc and msg_type in ('CMN.11350', 'CMN.11010'):
            return DECISION_CODE_KIND.get(dc, base)
    return base

STATUS_FROM_KIND: dict[str, str] = {
    'registered':  'FILED',
    'released':    'RELEASED',
    'rejected':    'REJECTED',
    'examination': 'EXAMINATION',
    'hold':        'HOLD',
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
    'info':        'OTHER',
}


def match_hawb(msg: AltaInboxMessage) -> Optional[HouseWaybill]:
    """Подобрать HAWB для входящего сообщения.

    На рабочем сервере Альта обслуживает много workflow помимо CargoTrack,
    поэтому 99%+ inbox-сообщений нам не принадлежат. Матчинг возможен только
    через идентификаторы, которые мы сами породили при отправке.

    Стратегия (по убыванию надёжности):
    1. parsed_meta['initial_envelope'] → AltaQueueItem.envelope_id → hawb
       Самое надёжное: UUID Envelope нашего исходящего, на который таможня
       отвечает. Работает для большинства типов сообщений (CMN.00003 ACK,
       CMN.11337, ED.* ответы и т.д.).
    2. Построить customs_declaration_number из parsed_meta и искать HAWB
       у которой это поле уже заполнено (для случая когда мы повторно ловим
       сообщение по уже зарегистрированной ДТ).
    3. Старое поле waybill_number_raw — оставлено на случай если когда-нибудь
       найдётся тип сообщения с WayBillNumber в теле. Сейчас не наблюдалось.

    None — нормальный исход для чужих сообщений.
    """
    from cargo.models import AltaQueueItem

    parsed = msg.parsed_meta or {}

    # 1. По initial_envelope нашего исходящего пакета
    init = (parsed.get('initial_envelope') or '').strip()
    if init:
        item = (
            AltaQueueItem.objects
            .filter(envelope_id__iexact=init)
            .exclude(hawb=None)
            .select_related('hawb')
            .first()
        )
        if item and item.hawb:
            return item.hawb

    # 2. По собранному номеру ДТ — если кто-то уже его проставил
    decl = _build_declaration_number(parsed)
    if decl:
        hawb = (
            HouseWaybill.objects
            .filter(customs_declaration_number=decl)
            .first()
        )
        if hawb:
            return hawb

    # 3. Fallback — WayBillNumber из XML, если вдруг будет
    wn = (msg.waybill_number_raw or '').strip()
    if wn:
        return (
            HouseWaybill.objects
            .filter(hawb_number__iexact=wn)
            .first()
        )

    return None


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


def apply_status(msg: AltaInboxMessage, hawb: HouseWaybill) -> Optional[str]:
    """Применяет customs_status и (для регистрации/выпуска) customs_declaration_number.

    customs_declaration_number записывается прямым UPDATE минуя HouseWaybill.save(),
    потому что save() автостирает поле при отсутствии MAWB / неполном чек-листе
    документов. Зеркало в Sheets вызываем вручную после refresh_from_db.
    """
    parsed = msg.parsed_meta or {}
    decl_number = _build_declaration_number(parsed)

    # Если в сообщении есть № ДТ — пишем его в HAWB через прямой UPDATE
    if decl_number:
        from django.db import transaction
        with transaction.atomic():
            HouseWaybill.objects.filter(pk=hawb.pk).update(customs_declaration_number=decl_number)
        # Вручную дёрнем writeback в Sheets (т.к. UPDATE не триггерит post_save)
        try:
            from cargo.services.sheets.writeback import write_declaration
            hawb.refresh_from_db(fields=['customs_declaration_number'])
            write_declaration(hawb)
        except Exception:
            logger.exception('sheets writeback after declaration write failed')

    new_status = STATUS_FROM_KIND.get(msg.msg_kind)
    if not new_status:
        return None  # info — статус не трогаем

    # change_customs_status сам валидирует logistics_status. Если HAWB ещё не
    # в таможне — статус просто не применяем (но declaration уже записан выше).
    if hawb.logistics_status not in ('EXPORT_CUSTOMS', 'IMPORT_CUSTOMS'):
        return f'HAWB не в таможне (logistics_status={hawb.logistics_status})'

    try:
        err = hawb.change_customs_status(new_status, user=None)
        if err:
            return str(err)
    except Exception as e:
        logger.exception('change_customs_status failed for HAWB %s', hawb.pk)
        return str(e)
    return None


def emit_event(msg: AltaInboxMessage, hawb: HouseWaybill) -> None:
    """Создаёт HawbWorkflowEvent в таймлайне HAWB."""
    event_type = EVENT_TYPE_FROM_KIND.get(msg.msg_kind, 'OTHER')
    occurred = msg.prepared_at or msg.received_at or timezone.now()
    HawbWorkflowEvent.objects.update_or_create(
        hawb=hawb,
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


def dispatch(msg: AltaInboxMessage) -> None:
    """Главная точка входа: матчинг → статус → событие → sheets writeback."""
    msg.msg_kind = classify(msg.msg_type, msg.parsed_meta)

    hawb = match_hawb(msg)
    if hawb:
        msg.hawb = hawb
        err = apply_status(msg, hawb)
        if err:
            msg.parsed_meta = {**(msg.parsed_meta or {}), 'apply_error': err}
            msg.status_applied = False
        else:
            msg.status_applied = True
            emit_event(msg, hawb)
            trigger_sheets_writeback(hawb)
    msg.save(update_fields=['msg_kind', 'hawb', 'status_applied', 'parsed_meta'])
