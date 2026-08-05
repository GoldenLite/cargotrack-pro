"""Мягкая архивация HAWB, пропавших из «Общее» («не прилетела»).

Бизнес-правило (юзер, 05.08.2026): менеджеры узнают ПОСТФАКТУМ, что посылка не
приехала, и просто убирают её строку/номер из «Общее». Это сигнал «удалить
накладную из партии». Удаление ОБРАТИМОЕ (soft): отвязать HAWB от партии
(mawb=None) + убрать из CRM-вкладок + записать аудит-событие. Саму запись
HouseWaybill СОХРАНЯЕМ → восстановление = вернуть строку в «Общее» (импорт свяжет
по номеру/ТСД заново).

Сигнал приходит из importer._sync_delete: stale general ImportedSheetRow =
строки, которых больше нет в «Общее».

ПРЕДОХРАНИТЕЛИ (удаление необратимых данных недопустимо):
1. ТОЛЬКО нулевой таможенный след (нет decl/release/filed/attempts/inbox). Если
   след есть — груз растаможен, исчезновение строки = ошибка (сорт/чистка) →
   НЕ трогаем, пишем warning «проверьте вручную».
2. Абсолютный кап ARCHIVE_CAP: слишком много кандидатов за прогон = глюк листа /
   массовая правка, а не N неприехавших → ПРОПУСКАЕМ всё, error-алерт. (Плюс
   относительный 10%-порог уже стоит в importer._sync_delete до вызова сюда.)
3. verify_absent: перепроверяем, что HAWB реально нет в «Общее» (для ручного
   вызова; из импортёра stale и так гарантированно отсутствует).
"""
import logging

from django.core.management import call_command
from django.utils import timezone

logger = logging.getLogger('cargo.archive')

ARCHIVE_CAP = 25


def _has_customs_footprint(h) -> bool:
    return bool(
        h.customs_declaration_number or h.release_date
        or getattr(h, 'filed_date', None)
        or h.declaration_attempts.exists()
        or h.inbox_messages.exists()
    )


def _general_hawb_set() -> set:
    """Множество HAWB-номеров, реально присутствующих в «Общее» сейчас."""
    from cargo.models import SheetSource
    from cargo.services.sheets.client import open_worksheet
    gen = SheetSource.objects.filter(kind='general', is_active=True).first()
    if not gen:
        return set()
    ws = open_worksheet(gen)
    vals = ws.get_all_values()
    headers = vals[gen.header_row - 1]
    col = next((i for i, hh in enumerate(headers)
                if hh.strip() in ('Накладная СДЭК', 'Номер накладной', 'HAWB')), None)
    if col is None:
        return set()
    return {vals[ri][col].strip() for ri in range(gen.header_row, len(vals))
            if col < len(vals[ri]) and vals[ri][col].strip()}


def archive_missing_general_hawbs(hawb_numbers, apply=True, verify_absent=True) -> dict:
    """Отвязать от партии HAWB, пропавшие из «Общее» (с предохранителями).

    hawb_numbers  — кандидаты (номера, пропавшие из «Общее»).
    apply         — False = dry-run (только показать, кого бы тронули).
    verify_absent — True = дополнительно убедиться, что HAWB нет в «Общее»
                    (для ручного вызова; из импортёра можно False).
    """
    from cargo.models import HouseWaybill, HawbWorkflowEvent

    nums = {str(x).strip() for x in hawb_numbers if str(x).strip()}
    if not nums:
        return {'archived': [], 'skipped_footprint': [], 'aborted': False}

    present = _general_hawb_set() if verify_absent else set()

    candidates, skipped_footprint, skipped_present, skipped_nolink = [], [], [], []
    for hn in nums:
        if hn in present:
            skipped_present.append(hn)
            continue
        h = HouseWaybill.objects.filter(hawb_number=hn).first()
        if not h or h.mawb_id is None:
            skipped_nolink.append(hn)   # нет в БД или уже без партии — нечего архивировать
            continue
        if _has_customs_footprint(h):
            skipped_footprint.append(hn)
            logger.warning('archive_missing: %s пропала из «Общее», НО есть '
                           'таможенный след (ДТ/выпуск/сообщения) — НЕ трогаю, '
                           'проверьте вручную', hn)
            continue
        candidates.append(h)

    # Предохранитель #2: абсолютный кап.
    if len(candidates) > ARCHIVE_CAP:
        logger.error('archive_missing: кандидатов %d > cap %d — ПРОПУСКАЮ архивацию '
                     '(похоже на глюк листа/массовую правку, вряд ли столько '
                     'неприехавших). Разобрать вручную.', len(candidates), ARCHIVE_CAP)
        return {'archived': [], 'skipped_footprint': skipped_footprint,
                'aborted': True, 'candidates': len(candidates)}

    if not apply:
        return {'archived': [h.hawb_number for h in candidates],
                'skipped_footprint': skipped_footprint,
                'skipped_present': skipped_present, 'dry': True, 'aborted': False}

    archived = []
    now = timezone.now()
    for h in candidates:
        awb = h.mawb.awb_number if h.mawb else '?'
        # Прямой update (без save()) — только отвязка, без побочных эффектов save().
        HouseWaybill.objects.filter(pk=h.pk).update(mawb=None, scan_into_bond=None)
        try:
            HawbWorkflowEvent.objects.get_or_create(
                hawb=h, event_type='OTHER', source_row=None,
                defaults={'occurred_at': now, 'source': 'sheet',
                          'comment': f'Не прилетела: убрана из «Общее» → '
                                     f'отвязана от партии {awb}'})
        except Exception:
            logger.exception('archive_missing: аудит-событие для %s не записалось',
                             h.hawb_number)
        logger.info('archive_missing: %s отвязана от партии %s '
                    '(не прилетела, убрана из «Общее»)', h.hawb_number, awb)
        archived.append(h.hawb_number)

    # Чистка CRM-вкладок (deleteDimension + индекс).
    if archived:
        try:
            call_command('delete_hawb_rows', *archived)
        except Exception:
            logger.exception('archive_missing: delete_hawb_rows (CRM cleanup) failed')

    logger.info('archive_missing: архивировано %d, пропущено(след)=%d, (в листе)=%d',
                len(archived), len(skipped_footprint), len(skipped_present))
    return {'archived': archived, 'skipped_footprint': skipped_footprint,
            'skipped_present': skipped_present, 'aborted': False}
