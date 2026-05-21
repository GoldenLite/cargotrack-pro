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


# ─── маппинг ED-кодов на наш semantic kind ──
# Заполняется после получения реальных .gz примеров. Сейчас — заглушка;
# всё что не в карте → kind='info' без изменения статуса.
# Источник: wiki.alta.ru/index.php/Структура_SQL-таблиц_ГТД
MSG_KIND_MAP: dict[str, str] = {
    # 'ED.1002001': 'registered',   # регистрация ДТ — TODO: уточнить
    # 'ED.1002005': 'released',     # выпуск ДТ — TODO: уточнить
    # 'ED.1002007': 'rejected',     # отказ — TODO: уточнить
    # 'ED.1002009': 'examination',  # досмотр — TODO: уточнить
    # 'ED.1002012': 'hold',         # запрос таможни — TODO: уточнить
}

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


def classify(msg_type: str) -> str:
    """ED.xxx → kind. Неизвестное → 'info'."""
    return MSG_KIND_MAP.get((msg_type or '').strip(), 'info')


def match_hawb(msg: AltaInboxMessage) -> Optional[HouseWaybill]:
    """Найти HAWB по WayBillNumber из XML."""
    wn = (msg.waybill_number_raw or '').strip()
    if not wn:
        return None
    return (
        HouseWaybill.objects
        .filter(hawb_number__iexact=wn)
        .first()
    )


def apply_status(msg: AltaInboxMessage, hawb: HouseWaybill) -> Optional[str]:
    """Применяет customs_status по маппингу. Возвращает текст ошибки или None.

    Использует HouseWaybill.change_customs_status() — у него своя валидация
    (вес HAWB ≤ MAWB и т.п.). Если поднимет ValueError — возвращаем
    сообщение, но НЕ ломаем dispatch.
    """
    new_status = STATUS_FROM_KIND.get(msg.msg_kind)
    if not new_status:
        return None  # info / неизвестный kind — статус не трогаем

    # change_customs_status сам валидирует логистический статус (требует
    # IMPORT_CUSTOMS / EXPORT_CUSTOMS); если HAWB не в таможне — пропускаем.
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
    msg.msg_kind = classify(msg.msg_type)

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
