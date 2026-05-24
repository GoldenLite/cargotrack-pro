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

from cargo.models import AltaInboxMessage, Cargo, HawbWorkflowEvent, HouseWaybill


logger = logging.getLogger('cargo.alta.inbox')


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
    'CMN.11314': 'info',         # Закрытие процедуры (DO1Close)
    'CMN.13021': 'info',         # DO1KeepLimits — лимит хранения / размещение на СВХ
    'CMN.13029': 'svh_placed',   # WHDocInventory — представление в таможню с MAWB.
                                 # Якорь (DocumentID) — для связи с CMN.13010.
    'CMN.13010': 'svh_do1_registered',  # DORegInfo — РЕАЛЬНАЯ регистрация ДО1.
                                        # Дата размещения + рег.номер ДО1. Связь с партией
                                        # через RefDocumentID → CMN.13029.DocumentID → Cargo.
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
    if base in ('svh_placed', 'svh_do1_registered'):
        lic = (parsed_meta.get('svh_warehouse_license') or '').strip()
        if lic and lic != OUR_WAREHOUSE_LICENSE:
            return 'info'
        # ДО2 (FormReport=2) тоже приходит как CMN.13010 — пока не интересует
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
    'svh_placed':  'OTHER',
    'info':        'OTHER',
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

    qs = AltaInboxMessage.objects.filter(
        cond,
        msg_kind__in=('released', 'withdrawn'),
    )
    latest = qs.order_by('-prepared_at', '-received_at').first()
    if not latest:
        return []

    if latest.msg_kind == 'withdrawn':
        target_decl = ''
    else:  # released
        target_decl = _build_declaration_number(latest.parsed_meta or {})
        if not target_decl:
            return []

    from django.db import transaction
    with transaction.atomic():
        current = HouseWaybill.objects.filter(pk=hawb.pk).values_list(
            'customs_declaration_number', flat=True).first() or ''
        if current == target_decl:
            return []
        HouseWaybill.objects.filter(pk=hawb.pk).update(
            customs_declaration_number=target_decl)

        # filed_date: дата подачи декларации = дата регистрации в таможне
        # (parsed_meta['registration_date'] из CMN-релиза). Ставим ОДИН раз
        # на пустое поле, через прямой UPDATE (минуя save()-автоочистки).
        # Writeback в Sheets отдельно — потому что direct UPDATE не дёргает
        # post_save сигнал.
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
    return [hawb]


def _writeback_hawbs(hawbs: list[HouseWaybill]) -> None:
    """Batch-writeback (decl + filed_date) для списка HAWB.

    Если signals_suppressed() — пропускаем (бэдчевая операция типа reparse
    отвечает за writeback сама в конце через resync_* команды).
    """
    if not hawbs:
        return
    try:
        from cargo.services.sheets.writeback import (
            batch_write_declarations_for_hawbs,
            batch_write_filed_dates_for_hawbs,
            signals_suppressed,
        )
        if signals_suppressed():
            return
        for h in hawbs:
            h.refresh_from_db(fields=['customs_declaration_number', 'filed_date'])
        batch_write_declarations_for_hawbs(hawbs)
        batch_write_filed_dates_for_hawbs(hawbs)
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

                # decl_number + filed_date: только при released/withdrawn
                # (recompute_declaration сам решает что взять как «истину»
                # из всей истории released/withdrawn сообщений по этой HAWB).
                # refresh_from_db: recompute пишет в DB через UPDATE минуя
                # save() — in-memory h.customs_declaration_number отстаёт,
                # без refresh последующий h.save() в change_customs_status
                # перетёр бы новый номер ОЛД-значением.
                if kind in ('released', 'withdrawn'):
                    recompute_declaration(cargo, h)
                    h.refresh_from_db(fields=[
                        'customs_declaration_number', 'filed_date',
                    ])

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
                except Exception as e:
                    logger.exception('change_customs_status failed for HAWB %s', h.pk)
                    errors.append(f'HAWB {h.hawb_number}: {e}')
    finally:
        if not in_bulk:
            end_batch_writeback()

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
    """Match CMN.13010 (регистрация ДО1) → Cargo по времени и лицензии.

    UUID-связи нет: RefDocumentID в ДО1 указывает на локальный документ
    Альта-СВХ («регистрация груза»), который мы не наблюдаем — этот
    промежуточный документ существует только внутри их софта.
    InitialEnvelopeID в обеих ED-цепочках — это наши собственные outbound
    envelope (CMN.13009 vs CMN.13029), разные UUID, тоже не помогают.

    Эвристика: для нашей лицензии в одной операции цепочка
        представление (CMN.13029)
            ↓ часы / иногда сутки
        регистрация груза (внутри Альта-СВХ)
            ↓
        ДО1 (CMN.13010)
    Представление всегда строго раньше ДО1. Окно — `LOOKBACK` (7 дней,
    с запасом под выходные между подачей представления и ДО1).

    Чтобы не привязать ОДНО представление к двум ДО1 — исключаем
    представления, у которых cargo уже имеет svh_do1_reg_number.
    «Ближайшее по времени» из оставшихся — наш кандидат.

    Возвращает (cargo, представление-сообщение).
    """
    from datetime import timedelta

    if not msg.prepared_at:
        return (None, None)

    LOOKBACK = timedelta(days=7)
    window_start = msg.prepared_at - LOOKBACK

    presentations = (
        AltaInboxMessage.objects
        .filter(
            msg_type='CMN.13029',
            msg_kind='svh_placed',  # уже отфильтровано по нашей лицензии в classify
            prepared_at__gte=window_start,
            prepared_at__lte=msg.prepared_at,
        )
        .exclude(cargo=None)
        .exclude(cargo__svh_do1_reg_number__gt='')  # уже привязан к другому ДО1
        .select_related('cargo')
        .order_by('-prepared_at')  # ближайшее раньше = первое
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

    НЕ пишет НИЧЕГО в Cargo. Семантика: партия «размещена на СВХ»
    только когда есть CMN.13010 (регистрация ДО1). До этого момента
    — заявка подана, ждём подтверждения таможни. В Sheets никаких
    СВХ-данных не показываем до факта размещения.

    Единственная задача — триггернуть backfill: если CMN.13010 для этой
    партии уже пришёл раньше представления (race), сейчас доматчим его.
    """
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


def dispatch(msg: AltaInboxMessage) -> None:
    """Главная точка входа: матчинг → recompute ДТ → статус → событие."""
    msg.msg_kind = classify(msg.msg_type, msg.parsed_meta)

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
        msg.save(update_fields=['status_applied', 'parsed_meta'])
    else:
        msg.save(update_fields=['msg_kind', 'cargo', 'hawb',
                                'status_applied', 'parsed_meta'])
