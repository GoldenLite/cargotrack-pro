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
# 10 — выпуск, 90 — отказ; остальные считаем info до выяснения.
DECISION_CODE_KIND: dict[str, str] = {
    '10': 'released',
    '11': 'released',
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

    Приоритет: Design > DecisionCode > ResolutionDescription > MessageType.
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
    return [hawb]


def _writeback_hawbs(hawbs: list[HouseWaybill]) -> None:
    if not hawbs:
        return
    try:
        from cargo.services.sheets.writeback import write_declaration
        for h in hawbs:
            h.refresh_from_db(fields=['customs_declaration_number'])
            write_declaration(h)
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

    # 1. Recompute для конкретной HAWB (если есть)
    updated_hawbs = recompute_declaration(cargo, hawb)
    _writeback_hawbs(updated_hawbs)

    # 2. Multi-waybill propagation: для release/withdrawn, если cargo известна,
    #    проходим по всем HAWB партии — раз release-сообщение CMN.11350 может
    #    содержать список PrDocumentNumber из нескольких HAWB-ов, и recompute
    #    их подхватит через raw_xml__icontains.
    if cargo and kind in ('released', 'withdrawn'):
        siblings = cargo.hawbs.all()
        if hawb:
            siblings = siblings.exclude(pk=hawb.pk)
        for sib in siblings:
            sib_updated = recompute_declaration(cargo, sib)
            _writeback_hawbs(sib_updated)

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

    errors = []
    applied = 0
    for h in targets:
        if h.logistics_status not in ('EXPORT_CUSTOMS', 'IMPORT_CUSTOMS'):
            errors.append(f'HAWB {h.hawb_number} не в таможне ({h.logistics_status})')
            continue
        try:
            err = h.change_customs_status(new_status, user=None)
            if err:
                errors.append(f'HAWB {h.hawb_number}: {err}')
            else:
                applied += 1
        except Exception as e:
            logger.exception('change_customs_status failed for HAWB %s', h.pk)
            errors.append(f'HAWB {h.hawb_number}: {e}')

    if applied == 0 and errors:
        return '; '.join(errors)
    return None


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
    """Match CMN.13010 (регистрация ДО1) → Cargo через цепочку:

    CMN.13010.RefDocumentID → CMN.13029.parsed_meta['svh_document_id']
                            → CMN.13029.cargo

    Возвращает (cargo, представление-сообщение). Любое может быть None
    если связь не сложилась (например представление пришло позже регистрации).
    """
    parsed = msg.parsed_meta or {}
    ref = (parsed.get('svh_ref_document_id') or '').strip()
    if not ref:
        return (None, None)

    # JSON-фильтр работает на Postgres и SQLite (Django 5+).
    presentation = (
        AltaInboxMessage.objects
        .filter(msg_type='CMN.13029', parsed_meta__svh_document_id=ref)
        .select_related('cargo')
        .order_by('-received_at')
        .first()
    )
    if presentation:
        return (presentation.cargo, presentation)
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

    Заполняет ТОЛЬКО лицензию — дата размещения и рег.номер берутся
    из CMN.13010 (apply_svh_do1), потому что представление != ДО1.

    Триггерит writeback (на случай если CMN.13010 для этой партии уже
    лежит в БД и привязка прокатит — типичный кейс при rematch).
    """
    parsed = msg.parsed_meta or {}
    license_ = (parsed.get('svh_warehouse_license') or '').strip()

    if license_ and not (cargo.warehouse_license or '').strip():
        cargo.warehouse_license = license_
        cargo.save(update_fields=['warehouse_license'])

    # Если CMN.13010 для этой партии уже пришла раньше (race), reparse её
    # тоже — иначе она навечно зависнет UNMATCHED.
    _backfill_do1_for_presentation(msg, cargo)

    _writeback_svh_cargo(cargo)
    return None


def _backfill_do1_for_presentation(presentation_msg: AltaInboxMessage,
                                   cargo: Cargo) -> None:
    """Если CMN.13010 пришла РАНЬШЕ представления — рематчит её сейчас.

    ДО1-сообщение знает ref_document_id, но связь с Cargo была невозможна
    пока представление не появилось в БД с привязкой. После того как
    представление matched → возвращаемся и подхватываем висящие ДО1.
    """
    doc_id = (presentation_msg.parsed_meta or {}).get('svh_document_id', '').strip()
    if not doc_id:
        return
    pending = AltaInboxMessage.objects.filter(
        msg_type='CMN.13010',
        parsed_meta__svh_ref_document_id=doc_id,
        cargo__isnull=True,
    )
    for do1 in pending:
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
        # Сохраняем привязки ДО apply_status: recompute_declaration читает
        # AltaInboxMessage.objects.filter(...) и должен увидеть и это сообщение.
        msg.save(update_fields=['msg_kind', 'cargo', 'hawb', 'parsed_meta'])

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
