"""Outbox observation: исходящие копии Альты, наблюдаемые агентом.

Альта складывает в `C:\\GTDSERV\\ED\\IN` не только входящие ответы таможни
(`serveralta^*.gz`), но и свои собственные исходящие копии (`538134^*.gz`).
Они дают нам единственный способ узнать какой EnvelopeID Альта присвоила
конкретной подаче — этот UUID потом возвращается в `InitialEnvelopeID`
входящих ответов и нужен для матчинга.

`dispatch(obs)` вызывается из view `api_alta_outbox_post` после
`update_or_create`. Делает только линковку obs → (Cargo, HAWB) по
вытащенным агентом полям `common_waybill_number` и `waybill_number`.
Сами входящие ответы матчатся уже в `inbox.match()` через эту таблицу.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from cargo.models import AltaOutboxObservation, Cargo, HouseWaybill


logger = logging.getLogger('cargo.alta.outbox')


def _find_cargo(common_wb: str) -> Optional[Cargo]:
    common_wb = (common_wb or '').strip()
    if not common_wb:
        return None
    return Cargo.objects.filter(awb_number__iexact=common_wb).first()


def _find_hawb(wb: str) -> Optional[HouseWaybill]:
    wb = (wb or '').strip()
    if not wb:
        return None
    return HouseWaybill.objects.filter(hawb_number__iexact=wb).first()


def link(obs: AltaOutboxObservation) -> Tuple[Optional[Cargo], Optional[HouseWaybill]]:
    """Привязывает наблюдение к Cargo и/или HAWB по вытащенным номерам.

    Для msg_type='ED.DO1' (DO1Report от Альты-СВХ) есть fallback: если MAWB
    в common_waybill_number не нашли Cargo — ищем по любому HAWB-номеру
    из parsed_meta['hawbs'] и берём его mawb.
    """
    cargo = _find_cargo(obs.common_waybill_number)
    hawb  = _find_hawb(obs.waybill_number)
    # Если HAWB найдена, а её mawb совпадает — заодно подтянем cargo.
    if hawb and not cargo and hawb.mawb_id:
        cargo = hawb.mawb
    # ED.DO1: fallback через список HAWB партии (parsed_meta['hawbs'])
    if not cargo and obs.msg_type == 'ED.DO1':
        for hawb_num in (obs.parsed_meta or {}).get('hawbs') or []:
            h = _find_hawb(hawb_num)
            if h and h.mawb_id:
                cargo = h.mawb
                break
    return cargo, hawb


def dispatch(obs: AltaOutboxObservation) -> None:
    """Линкует obs к Cargo/HAWB. Сохраняет только если хоть что-то нашли.

    Для msg_type='ED.DO1' дополнительно проставляет Cargo.svh_do1_sent_at =
    obs.prepared_at (момент когда Альта-СВХ отправила ДО-1 в таможню).
    """
    cargo, hawb = link(obs)
    update_fields = []
    if cargo and obs.cargo_id != cargo.pk:
        obs.cargo = cargo
        update_fields.append('cargo')
    if hawb and obs.hawb_id != hawb.pk:
        obs.hawb = hawb
        update_fields.append('hawb')
    if update_fields:
        obs.save(update_fields=update_fields)
        logger.info('outbox %s linked: cargo=%s hawb=%s', obs.envelope_id,
                    obs.cargo_id, obs.hawb_id)

    # ED.DO1: per-HAWB дата подачи ДО-1 + per-Cargo лицензия СВХ + per-HAWB
    # вес/места. Один ДО-1 может содержать только часть HAWB партии (если
    # подача разбита на несколько ДО-1 — например 222 + 186 = 408 на одну
    # MAWB). Поэтому svh_do1_sent_at пишется ТОЛЬКО в HAWB перечисленные
    # в parsed_meta['hawbs'], не во все HAWB cargo.
    if obs.msg_type == 'ED.DO1':
        # Auto-create Cargo если его нет в БД. В Sheets «Общее» MAWB-колонки
        # нет — auto-promote создаёт HAWB без mawb. ED.DO1 — единственный
        # достоверный источник MAWB↔HAWB связи (Альта-СВХ сама строит подачу).
        # Создаём минимальный Cargo (stage=DRAFT) и привязываем HAWB.
        if not cargo and obs.common_waybill_number:
            cargo = _ensure_cargo_from_do1(obs)
            if cargo:
                obs.cargo = cargo
                obs.save(update_fields=['cargo'])
        # Лицензия СВХ — общий атрибут партии. Заполняем только если ещё
        # не пришла из CMN.13010 (там сначала ставится через scan_into_bond).
        if cargo:
            cert = ((obs.parsed_meta or {}).get('certificate_number') or '').strip()
            if cert and not (cargo.warehouse_license or '').strip():
                Cargo.objects.filter(pk=cargo.pk).update(warehouse_license=cert[:50])
                logger.info('ED.DO1: Cargo %s warehouse_license=%s',
                            cargo.awb_number, cert)
                _writeback_svh_for_cargo(cargo)
        # Per-HAWB дата подачи: ставим только тем что в hawbs списке.
        hawb_nums = (obs.parsed_meta or {}).get('hawbs') or []
        if obs.prepared_at and hawb_nums:
            _apply_do1_sent_at(hawb_nums, obs.prepared_at)
        # Per-HAWB привязка к Cargo + writeback MAWB в Sheets.
        if cargo and hawb_nums:
            _link_hawbs_to_cargo(hawb_nums, cargo)
        # Per-HAWB вес/места из <Goods> блоков.
        goods = (obs.parsed_meta or {}).get('goods') or {}
        if goods:
            _apply_do1_goods(goods)

    # CMN.11023 (первичная подача ДТ) / CMN.11349 (корректировка) — содержат
    # точный момент подачи в таможню. Заполняем HouseWaybill.filed_date с
    # реальным временем (раньше там стояло 00:00:00 из CMN-RegistrationDate).
    # Приоритет: 11023 > 11349. Логика: пишем только если поле пустое ИЛИ
    # новое prepared_at раньше.
    #
    # Одна декларация на партию = N HAWB. Агент парсит весь список HAWB из
    # XML и передаёт в parsed_meta.hawbs. Если список есть — итерируемся
    # по каждой и проставляем filed_date. Fallback на старую логику (одна
    # waybill_number) — для observations созданных старым агентом.
    if obs.msg_type in ('CMN.11023', 'CMN.11349') and obs.prepared_at:
        hawb_list = (obs.parsed_meta or {}).get('hawbs') or []
        if hawb_list:
            _apply_filed_date_to_hawbs(hawb_list, obs.prepared_at)
        elif hawb:
            _maybe_update_filed_date(hawb, obs.prepared_at)

        # Количество позиций ДТ. Источник — parsed_meta:
        #   CMN.11023: goods_count (общее число для всей декларации) →
        #              ставится одинаково всем HAWB этой декларации.
        #   CMN.11349: goods_count_per_hawb (dict[hawb_num → int]) →
        #              ставится per-HAWB.
        # Если в parsed_meta пусто (старый агент) — пытаемся парсить raw_xml
        # на месте.
        _apply_goods_count(obs, hawb_list)

    # Экспортные сообщения: CMN.11335 (ПТДЭГ), CMN.11349 ЭК (ДТЭГ),
    # CMN.11024 ЭК (ДТ). Auto-create HAWB+Cargo, проставляем declaration_form,
    # filed_date, goods_count, транспортный документ, добавляем строку в
    # Sheets «Экспортная статистика».
    if obs.msg_type in ('CMN.11335', 'CMN.11349', 'CMN.11024'):
        _apply_export_outbox(obs)


def _apply_goods_count(obs, hawb_list: list) -> None:
    """Записывает HouseWaybill.goods_count из parsed_meta CMN.11023/11349."""
    pm = obs.parsed_meta or {}
    raw_xml = pm.get('raw_xml') or ''

    affected: list[HouseWaybill] = []

    if obs.msg_type == 'CMN.11023':
        total = pm.get('goods_count')
        if (total is None or total == 0) and raw_xml:
            from cargo.services.alta.xml_extract import count_positions_cmn_11023
            total = count_positions_cmn_11023(raw_xml) or None
        if not total:
            return
        # Один и тот же счётчик всем HAWB декларации
        for hn in (hawb_list or []):
            h = HouseWaybill.objects.filter(
                hawb_number__iexact=str(hn).strip()).first()
            if not h or h.goods_count == total:
                continue
            HouseWaybill.objects.filter(pk=h.pk).update(goods_count=total)
            affected.append(h)

    elif obs.msg_type == 'CMN.11349':
        per_hawb = pm.get('goods_count_per_hawb') or {}
        if not per_hawb and raw_xml:
            from cargo.services.alta.xml_extract import (
                count_positions_per_hawb_cmn_11349,
            )
            per_hawb = count_positions_per_hawb_cmn_11349(raw_xml)
        if not per_hawb:
            return
        for hn, n in per_hawb.items():
            if not n:
                continue
            h = HouseWaybill.objects.filter(
                hawb_number__iexact=str(hn).strip()).first()
            if not h or h.goods_count == n:
                continue
            HouseWaybill.objects.filter(pk=h.pk).update(goods_count=n)
            affected.append(h)

    if affected:
        logger.info('%s goods_count: updated %d HAWBs',
                    obs.msg_type, len(affected))
        try:
            from cargo.services.sheets.writeback import (
                batch_write_goods_count_for_hawbs, signals_suppressed,
            )
            if not signals_suppressed():
                for h in affected:
                    h.refresh_from_db(fields=['goods_count'])
                batch_write_goods_count_for_hawbs(affected)
        except Exception:
            logger.exception('goods_count writeback failed')


def _filed_date_should_replace(current, new) -> bool:
    """Решает, нужно ли заменить current на new для HouseWaybill.filed_date.

    Логика:
    - current пуст → да, всегда пишем.
    - new пуст или совпадает → нет.
    - current с time=00:00:00 в локальной TZ (значит дата без часов —
      пришла из CMN.11350.registration_date), а new — с реальным временем
      суток (CMN.11023/11349.prepared_at в МСК) → да, перезаписываем.
    - оба точные → берём более ранний.
    - оба ровно по дате — оставляем текущий.

    Точность определяется в локальной TZ (МСК): hour|minute|second|μs
    после timezone.localtime(). UTC-проверка ошибочна, т.к. 00:00 МСК
    в БД хранится как 21:00 UTC предыдущего дня.
    """
    if not current:
        return bool(new)
    if not new or current == new:
        return False
    from django.utils import timezone as _tz
    cur_local = _tz.localtime(current) if _tz.is_aware(current) else current
    new_local = _tz.localtime(new)     if _tz.is_aware(new)     else new
    current_precise = bool(cur_local.hour or cur_local.minute
                           or cur_local.second or cur_local.microsecond)
    new_precise     = bool(new_local.hour or new_local.minute
                           or new_local.second or new_local.microsecond)
    if not current_precise and new_precise:
        return True
    if current_precise and not new_precise:
        return False
    # одинаковая точность → берём более ранний
    return new < current


def _maybe_update_filed_date(hawb: HouseWaybill, prepared_at) -> None:
    """Обновляет HouseWaybill.filed_date если новое значение раньше или поле пустое.

    Дополнительно распространяет дату на ВСЕ HAWB с тем же
    customs_declaration_number (одна ДТ → одна дата подачи для всех её
    накладных). CMN.11023/11349 обычно приходит per-HAWB, но фактически
    все накладные одной ДТ подаются одновременно.
    """
    hawb.refresh_from_db(fields=['filed_date', 'customs_declaration_number'])
    if _filed_date_should_replace(hawb.filed_date, prepared_at):
        HouseWaybill.objects.filter(pk=hawb.pk).update(filed_date=prepared_at)
        logger.info('filed_date: HAWB %s set to %s', hawb.hawb_number, prepared_at)
        _writeback_filed_date(hawb)
    # Propagate to siblings with same ДТ
    decl = (hawb.customs_declaration_number or '').strip()
    if decl:
        siblings = HouseWaybill.objects.filter(
            customs_declaration_number=decl
        ).exclude(pk=hawb.pk)
        affected = []
        for sib in siblings:
            if sib.filed_date and sib.filed_date <= prepared_at:
                continue
            HouseWaybill.objects.filter(pk=sib.pk).update(filed_date=prepared_at)
            affected.append(sib)
        if affected:
            logger.info('filed_date propagation: %d siblings of ДТ %s set to %s',
                        len(affected), decl, prepared_at)
            for sib in affected:
                _writeback_filed_date(sib)


def _apply_do1_goods(goods: dict) -> None:
    """Записывает per-HAWB вес и места из ДО-1 Goods блоков.

    goods: {'10257142180': {'weight': '0.062', 'places': 1}, ...}
    Идемпотентно — обновляет только если значение изменилось.
    """
    from decimal import Decimal, InvalidOperation
    affected: list[HouseWaybill] = []
    for hawb_num, data in goods.items():
        h = HouseWaybill.objects.filter(hawb_number__iexact=hawb_num).first()
        if not h:
            continue
        try:
            new_weight = Decimal(str(data.get('weight') or '0'))
        except (InvalidOperation, ValueError):
            new_weight = None
        new_places = data.get('places') or None
        update_fields = {}
        if new_weight is not None and h.svh_do1_gross_weight != new_weight:
            update_fields['svh_do1_gross_weight'] = new_weight
        if new_places is not None and h.svh_do1_place_count != new_places:
            update_fields['svh_do1_place_count'] = new_places
        if update_fields:
            HouseWaybill.objects.filter(pk=h.pk).update(**update_fields)
            affected.append(h)
    if affected:
        logger.info('ED.DO1 goods: updated %d HAWBs (weight/places)', len(affected))
        try:
            from cargo.services.sheets.writeback import (
                batch_write_svh_do1_weight_for_hawbs,
                batch_write_svh_do1_places_for_hawbs,
                signals_suppressed,
            )
            if not signals_suppressed():
                for h in affected:
                    h.refresh_from_db(fields=['svh_do1_gross_weight', 'svh_do1_place_count'])
                batch_write_svh_do1_weight_for_hawbs(affected)
                batch_write_svh_do1_places_for_hawbs(affected)
        except Exception:
            logger.exception('svh_do1 weight/places writeback failed')


def _writeback_filed_date(hawb: HouseWaybill) -> None:
    """Sync ячейки «дата подачи» в Sheets для одного HAWB."""
    try:
        from cargo.services.sheets.writeback import (
            write_filed_date_for_hawb, signals_suppressed,
        )
        if signals_suppressed():
            return
        hawb.refresh_from_db(fields=['filed_date'])
        write_filed_date_for_hawb(hawb)
    except Exception:
        logger.exception('filed_date writeback failed')


def _apply_do1_sent_at(hawb_nums: list, prepared_at) -> None:
    """Per-HAWB svh_do1_sent_at — только для HAWB-номеров из ДО-1.

    Если у HAWB уже стоит более раннее значение (от предыдущего ДО-1 на эту
    же накладную) — оставляем; иначе перезаписываем. Юзер: «дата подачи
    ДО-1» = когда МЫ отправили.
    """
    affected: list[HouseWaybill] = []
    for hawb_num in hawb_nums:
        h = HouseWaybill.objects.filter(hawb_number__iexact=str(hawb_num).strip()).first()
        if not h:
            continue
        if h.svh_do1_sent_at and h.svh_do1_sent_at <= prepared_at:
            continue  # уже стоит более ранний ДО-1
        HouseWaybill.objects.filter(pk=h.pk).update(svh_do1_sent_at=prepared_at)
        affected.append(h)
    if affected:
        logger.info('ED.DO1 sent_at: updated %d HAWBs to %s (БД, без Sheets)',
                    len(affected), prepared_at)
        # Колонка «CargoTrack: дата подачи ДО1» удалена 2026-05-26 (юзер не
        # использует). Поле svh_do1_sent_at в БД остаётся для внутренней
        # логики, но в Sheets больше не пишем.


def _apply_filed_date_to_hawbs(hawb_nums: list, prepared_at) -> None:
    """Per-HAWB filed_date — для каждой накладной из CMN.11023/11349 hawbs списка.

    Одна декларация на партию = N HAWB. Агент парсит весь список из XML
    и передаёт в parsed_meta.hawbs. Здесь итерируемся и для каждой HAWB
    проставляем filed_date (если ещё не стоит или новое время раньше).
    """
    from cargo.services.sheets.writeback import (
        batch_write_filed_dates_for_hawbs, signals_suppressed,
    )
    affected: list[HouseWaybill] = []
    for hn in hawb_nums:
        h = HouseWaybill.objects.filter(
            hawb_number__iexact=str(hn).strip()
        ).first()
        if not h:
            continue
        if not _filed_date_should_replace(h.filed_date, prepared_at):
            continue
        HouseWaybill.objects.filter(pk=h.pk).update(filed_date=prepared_at)
        affected.append(h)
    if affected:
        logger.info('CMN.11023/11349 filed_date: updated %d HAWBs to %s',
                    len(affected), prepared_at)
        if not signals_suppressed():
            try:
                for h in affected:
                    h.refresh_from_db(fields=['filed_date'])
                batch_write_filed_dates_for_hawbs(affected)
            except Exception:
                logger.exception('filed_date writeback failed')


def _ensure_cargo_from_do1(obs: AltaOutboxObservation) -> Optional[Cargo]:
    """Auto-create минимальный Cargo для ED.DO1 если его ещё нет в БД.

    В Sheets «Общее» MAWB-колонки нет — auto-promote создаёт HAWB без
    привязки к партии. ED.DO1 от нашего Альта-СВХ — единственный достоверный
    источник связи MAWB↔HAWB-список (мы сами строим этот документ).

    Создаётся минимальная партия (stage='DRAFT'), остальные поля юзер
    заполняет вручную при необходимости.
    """
    mawb = (obs.common_waybill_number or '').strip()
    if not mawb:
        return None
    try:
        cargo = Cargo.objects.create(awb_number=mawb, stage='DRAFT')
        logger.info('ED.DO1: auto-created Cargo %s (stage=DRAFT)', mawb)
        return cargo
    except Exception:
        logger.exception('failed to auto-create Cargo %s', mawb)
        return Cargo.objects.filter(awb_number__iexact=mawb).first()


def _link_hawbs_to_cargo(hawb_nums: list, cargo: Cargo) -> None:
    """Привязывает HAWB из ED.DO1 к Cargo (если они в БД и без mawb).

    Использует прямой UPDATE минуя save() — иначе HouseWaybill.save()
    делает валидацию `logistics_status == JOINABLE_STATUS` (AT_ORIGIN_WH).
    HAWB-ы которые юзер видит из ED.DO1 уже физически на нашем СВХ,
    их logistics_status может быть любым.

    После привязки — пишет MAWB в новую CargoTrack-колонку «номер партии»
    в Sheets (если writeback не подавлен bulk-режимом).
    """
    affected: list[HouseWaybill] = []
    for hawb_num in hawb_nums:
        h = HouseWaybill.objects.filter(
            hawb_number__iexact=str(hawb_num).strip()
        ).first()
        if not h or h.mawb_id == cargo.pk:
            continue
        if h.mawb_id and h.mawb_id != cargo.pk:
            logger.warning(
                'ED.DO1: HAWB %s уже в партии %s, не перепривязываю к %s',
                h.hawb_number, h.mawb.awb_number, cargo.awb_number)
            continue
        HouseWaybill.objects.filter(pk=h.pk).update(mawb_id=cargo.pk)
        affected.append(h)
    if affected:
        logger.info('ED.DO1: linked %d HAWBs to Cargo %s',
                    len(affected), cargo.awb_number)
        try:
            from cargo.services.sheets.writeback import (
                batch_write_cargo_mawb_for_hawbs, signals_suppressed,
            )
            if not signals_suppressed():
                for h in affected:
                    h.refresh_from_db(fields=['mawb_id'])
                batch_write_cargo_mawb_for_hawbs(affected)
        except Exception:
            logger.exception('cargo_mawb writeback failed')


def _writeback_svh_for_cargo(cargo: Cargo) -> None:
    """Sync лицензии/даты ДО1/рег.номера ДО1 в Sheets для одной партии."""
    try:
        from cargo.services.sheets.writeback import (
            batch_write_svh_for_cargos, signals_suppressed,
        )
        if signals_suppressed():
            return
        cargo.refresh_from_db(fields=['warehouse_license', 'scan_into_bond',
                                       'svh_do1_reg_number'])
        batch_write_svh_for_cargos([cargo])
    except Exception:
        logger.exception('svh writeback failed for cargo %s', cargo.awb_number)



def relink_for_cargo(cargo: Cargo) -> int:
    """После появления нового Cargo — допривязать к нему уже накопленные
    AltaOutboxObservation с тем же CommonWayBillNumber. Возвращает счётчик.
    """
    awb = (cargo.awb_number or '').strip()
    if not awb:
        return 0
    return (
        AltaOutboxObservation.objects
        .filter(common_waybill_number__iexact=awb, cargo=None)
        .update(cargo=cargo)
    )


def relink_for_hawb(hawb: HouseWaybill) -> int:
    """Аналогично для HAWB по WayBillNumber."""
    wb = (hawb.hawb_number or '').strip()
    if not wb:
        return 0
    return (
        AltaOutboxObservation.objects
        .filter(waybill_number__iexact=wb, hawb=None)
        .update(hawb=hawb)
    )


# ─── Экспортные исходящие сообщения ────────────────────────────────────

_DECL_FORM_BY_MSG_TYPE = {
    'CMN.11335': 'ПТДЭГ',
    'CMN.11349': 'ДТЭГ',
    'CMN.11024': 'ДТ',
}


def _parse_export_obs(obs: AltaOutboxObservation) -> Optional[dict]:
    """Парсит raw_xml observation в зависимости от msg_type.

    Возвращает dict с ключами:
      'is_export': bool,
      'hawbs':     list[str],
      'transport_per_hawb': {hawb: transport_doc} или {} для CMN.11024,
      'goods_count_per_hawb': {hawb: int}    — для CMN.11335/11349,
      'goods_count':          int            — для CMN.11024 (один на ДТ).

    None если raw_xml пуст или ничего не вытащили.
    """
    raw_xml = (obs.parsed_meta or {}).get('raw_xml') or ''
    if not raw_xml:
        return None

    from cargo.services.alta.xml_extract import (
        parse_cmn_11335, parse_cmn_11024, parse_cmn_11349_meta,
    )
    if obs.msg_type == 'CMN.11335':
        r = parse_cmn_11335(raw_xml)
        return {
            'is_export':            (r['declaration_kind'] or '').strip() == 'ЭК',
            'hawbs':                r['hawbs'],
            'transport_per_hawb':   r['transport_per_hawb'],
            'goods_count_per_hawb': r['goods_count_per_hawb'],
            'goods_count':          0,
        }
    if obs.msg_type == 'CMN.11349':
        r = parse_cmn_11349_meta(raw_xml)
        return {
            'is_export':            (r['declaration_kind'] or '').strip() == 'ЭК',
            'hawbs':                r['hawbs'],
            'transport_per_hawb':   r['transport_per_hawb'],
            'goods_count_per_hawb': r['goods_count_per_hawb'],
            'goods_count':          0,
        }
    if obs.msg_type == 'CMN.11024':
        r = parse_cmn_11024(raw_xml)
        return {
            'is_export':            (r['customs_procedure'] or '').strip() == 'ЭК',
            'hawbs':                r['hawbs'],
            'transport_per_hawb':   r['transport_per_hawb'],
            'goods_count_per_hawb': {},
            'goods_count':          r['goods_count'],
        }
    return None


def _ensure_export_cargo(awb_number: str) -> Optional[Cargo]:
    """Auto-create Cargo для экспортного транспортного документа (CDEK-XX-NNNN)."""
    awb_number = (awb_number or '').strip()
    if not awb_number:
        return None
    cargo = Cargo.objects.filter(awb_number__iexact=awb_number).first()
    if cargo:
        return cargo
    try:
        cargo = Cargo.objects.create(awb_number=awb_number, stage='DRAFT')
        logger.info('export: auto-created Cargo %s', awb_number)
        return cargo
    except Exception:
        logger.exception('export: failed to create Cargo %s', awb_number)
        return Cargo.objects.filter(awb_number__iexact=awb_number).first()


def _ensure_export_hawb(hawb_number: str, cargo: Optional[Cargo]
                        ) -> Optional[HouseWaybill]:
    """Auto-create HouseWaybill(shipment_type='EXPORT') если ещё нет в БД.

    HouseWaybill.save() запрещает привязку к mawb при статусе ≠ AT_ORIGIN_WH.
    Для экспортной HAWB которая сразу идёт через таможню это правило не
    нужно — обходим через прямой UPDATE: создаём HAWB без mawb, потом
    UPDATE mawb_id отдельным запросом минуя save().
    """
    hawb_number = (hawb_number or '').strip()
    if not hawb_number:
        return None
    h = HouseWaybill.objects.filter(hawb_number__iexact=hawb_number).first()
    if not h:
        try:
            h = HouseWaybill.objects.create(
                hawb_number=hawb_number,
                shipment_type='EXPORT',
                logistics_status='EXPORT_CUSTOMS',
            )
            logger.info('export: auto-created HAWB %s (EXPORT_CUSTOMS)',
                        hawb_number)
        except Exception:
            logger.exception('export: failed to create HAWB %s', hawb_number)
            return HouseWaybill.objects.filter(
                hawb_number__iexact=hawb_number).first()
    # Привязка к Cargo — прямым UPDATE минуя save() и валидацию статусов.
    if cargo and h.mawb_id != cargo.pk and not h.mawb_id:
        HouseWaybill.objects.filter(pk=h.pk).update(mawb_id=cargo.pk)
        h.refresh_from_db(fields=['mawb'])
    return h


def _apply_export_outbox(obs: AltaOutboxObservation) -> None:
    """Обработка одного экспортного outbox-сообщения (CMN.11335/11349/11024).

    Шаги:
      1. Парсим raw_xml — проверяем что это ЭК.
      2. Auto-create Cargo (транспортный документ) и HAWB(EXPORT_CUSTOMS).
      3. Проставляем declaration_form, filed_date, goods_count.
      4. _ensure_export_row для каждой HAWB.
      5. Batch writeback declaration_form, transport_doc, goods_count + всё
         что уже есть на HAWB (declaration, release_date, requests, attempts).
    """
    parsed = _parse_export_obs(obs)
    if not parsed:
        return  # raw_xml нет или unknown msg_type
    if not parsed['is_export']:
        return  # ЭК не подтверждён — это импортное сообщение, не наш case

    decl_form = _DECL_FORM_BY_MSG_TYPE.get(obs.msg_type, '')

    affected: list[HouseWaybill] = []
    for hawb_num in parsed['hawbs']:
        transport_doc = parsed['transport_per_hawb'].get(hawb_num, '')
        cargo = _ensure_export_cargo(transport_doc) if transport_doc else None
        h = _ensure_export_hawb(hawb_num, cargo)
        if not h:
            continue

        update_fields: dict = {}
        if decl_form and h.declaration_form != decl_form:
            update_fields['declaration_form'] = decl_form
        if obs.prepared_at and _filed_date_should_replace(
                h.filed_date, obs.prepared_at):
            update_fields['filed_date'] = obs.prepared_at
        per_hawb_count = parsed['goods_count_per_hawb'].get(hawb_num) \
            or parsed['goods_count']
        if per_hawb_count and h.goods_count != per_hawb_count:
            update_fields['goods_count'] = per_hawb_count
        if update_fields:
            HouseWaybill.objects.filter(pk=h.pk).update(**update_fields)
            h.refresh_from_db(fields=list(update_fields.keys()) + ['mawb'])
        affected.append(h)

    if not affected:
        return

    logger.info('%s ЭК: applied to %d HAWBs, decl_form=%s',
                obs.msg_type, len(affected), decl_form)
    _writeback_export_hawbs(affected)


def _writeback_export_hawbs(hawbs: list) -> None:
    """Все batch writeback для экспортных HAWB в одну вкладку."""
    try:
        from cargo.services.sheets.writeback import (
            ensure_export_rows_for_hawbs,
            batch_write_declarations_for_hawbs,
            batch_write_filed_dates_for_hawbs,
            batch_write_release_dates_for_hawbs,
            batch_write_goods_count_for_hawbs,
            batch_write_customs_requests_for_hawbs,
            batch_write_customs_requests_count_for_hawbs,
            batch_write_attempts_count_for_hawbs,
            batch_write_transport_doc_for_hawbs,
            batch_write_declaration_form_for_hawbs,
            batch_write_ed_status_for_hawbs,
            signals_suppressed,
        )
        if signals_suppressed():
            return
        ensure_export_rows_for_hawbs(hawbs)
        batch_write_transport_doc_for_hawbs(hawbs)
        batch_write_declarations_for_hawbs(hawbs)
        batch_write_filed_dates_for_hawbs(hawbs)
        batch_write_release_dates_for_hawbs(hawbs)
        batch_write_goods_count_for_hawbs(hawbs)
        batch_write_customs_requests_for_hawbs(hawbs)
        batch_write_customs_requests_count_for_hawbs(hawbs)
        batch_write_attempts_count_for_hawbs(hawbs)
        batch_write_declaration_form_for_hawbs(hawbs)
        batch_write_ed_status_for_hawbs(hawbs)
    except Exception:
        logger.exception('export writeback failed')
