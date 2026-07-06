"""Обработка входящих вебхуков СДЭК (ORDER_STATUS).

Поток (вызывается из view cdek_webhook сразу после приёма JSON):
1. parse_payload — валидируем тип ORDER_STATUS, достаём uuid + attributes.
2. dispatch:
   - по uuid делаем АВТОРИТЕТНЫЙ до-запрос GET /v2/orders/{uuid}. Это:
     (а) защита от спуфинга (у вебхука нет подписи),
     (б) единственный надёжный способ узнать im_number (=наш hawb_number) —
         тело вебхука его не гарантирует,
     (в) полная история статусов.
   - матчим HAWB по im_number, применяем applier.apply_status_to_hawb.
   - если до-запрос не удался — fallback на данные из тела вебхука
     (матчим по сохранённому cdek_number), чтобы не потерять апдейт.

Аналог cargo/services/alta/inbox.py dispatch().
"""
from __future__ import annotations

import logging
from typing import Optional

from cargo.models import HouseWaybill
from . import applier
from .client import CdekClient, extract_statuses


logger = logging.getLogger('cargo.cdek.webhook')


def parse_payload(data: dict) -> Optional[dict]:
    """Валидирует ORDER_STATUS-вебхук, возвращает нормализованный dict | None."""
    if not isinstance(data, dict):
        return None
    if (data.get('type') or '').strip().upper() != 'ORDER_STATUS':
        return None
    attrs = data.get('attributes') or {}
    return {
        'uuid': (data.get('uuid') or '').strip(),
        'cdek_number': str(attrs.get('cdek_number') or '').strip(),
        'number': (attrs.get('number') or '').strip(),  # часто отсутствует
        'status_code': (attrs.get('status_code') or attrs.get('code') or '').strip(),
        'status_date_time': (attrs.get('status_date_time') or '').strip(),
        'city_name': (attrs.get('city_name') or '').strip(),
    }


def _apply_from_authoritative(parsed: dict, client: CdekClient) -> Optional[dict]:
    """До-запрос по uuid → match по im_number → apply. None если не вышло."""
    entity = client.get_order_by_uuid(parsed['uuid'])
    if not entity:
        return None
    ext = extract_statuses(entity)
    hawb = applier.resolve_hawb(ext.get('number') or '')
    if not hawb and ext.get('cdek_number'):
        hawb = HouseWaybill.objects.filter(cdek_number=ext['cdek_number']).first()
    if not hawb:
        logger.info('cdek webhook: order uuid=%s (im_number=%s) не сматчен с HAWB',
                    parsed['uuid'], ext.get('number'))
        return {'matched': False, 'changed': False, 'hawb_id': None}
    changed = applier.apply_status_to_hawb(hawb, ext, source='webhook')
    return {'matched': True, 'changed': changed, 'hawb_id': hawb.pk}


def _apply_from_body(parsed: dict) -> dict:
    """Fallback: применяем единственный статус из тела вебхука.

    Матчим по сохранённому cdek_number (number в теле обычно нет). Если не
    нашли — оставляем unmatched, reconcile подберёт позже.
    """
    hawb = None
    if parsed.get('number'):
        hawb = applier.resolve_hawb(parsed['number'])
    if not hawb and parsed.get('cdek_number'):
        hawb = HouseWaybill.objects.filter(cdek_number=parsed['cdek_number']).first()
    if not hawb:
        logger.info('cdek webhook fallback: cdek_number=%s не сматчен',
                    parsed.get('cdek_number'))
        return {'matched': False, 'changed': False, 'hawb_id': None}

    code = parsed.get('status_code') or ''
    single = {
        'uuid': parsed.get('uuid') or '',
        'cdek_number': parsed.get('cdek_number') or '',
        'number': parsed.get('number') or hawb.hawb_number,
        'statuses': [{
            'code': code,
            'name': '',
            'date_time': parsed.get('status_date_time') or '',
            'city': parsed.get('city_name') or '',
        }],
        'current': {
            'code': code,
            'name': '',
            'date_time': parsed.get('status_date_time') or '',
            'city': parsed.get('city_name') or '',
        },
    }
    changed = applier.apply_status_to_hawb(hawb, single, source='webhook')
    return {'matched': True, 'changed': changed, 'hawb_id': hawb.pk}


def dispatch(parsed: dict, *, client: Optional[CdekClient] = None,
             reverify: bool = True) -> dict:
    """Главная точка входа обработки ORDER_STATUS-вебхука."""
    if not parsed or not parsed.get('uuid'):
        return {'matched': False, 'changed': False, 'hawb_id': None,
                'reason': 'no uuid'}

    close_after = False
    if reverify and client is None:
        try:
            client = CdekClient()
            close_after = True
        except Exception:
            logger.exception('cdek webhook: не удалось создать клиента')
            client = None

    try:
        if reverify and client is not None:
            try:
                res = _apply_from_authoritative(parsed, client)
                if res is not None:
                    return res
                logger.warning('cdek webhook: заказ uuid=%s не найден через API, '
                               'fallback на тело', parsed['uuid'])
            except Exception:
                logger.exception('cdek webhook: до-запрос по uuid=%s упал, '
                                 'fallback на тело', parsed['uuid'])
        # Fallback на данные из тела вебхука.
        return _apply_from_body(parsed)
    finally:
        if close_after and client is not None:
            client.close()
