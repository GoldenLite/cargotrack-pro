"""Сборщик health-статусов для Telegram /status команды.

Возвращает текст готовый отправить в Telegram. Каждый модуль помечается
🟢 (норм), 🟡 (тревога — старые данные), 🔴 (мертво).

Пороги:
- alta_agent inbox/outbox: 🟢 <5 мин, 🟡 5-30 мин, 🔴 >30 мин
- db_reconcile last cycle: 🟢 <10 мин, 🟡 10-60 мин, 🔴 >60 мин
- Deklarant: 🟢 активна + использовалась <24ч, 🟡 24-72ч, 🔴 неактивна
- Sheets writeback: 🟢 <5 мин, 🟡 5-60 мин (живой live), 🔴 >60 мин
- Orphan inbox / unapplied outbox: 🟢 <10, 🟡 10-100, 🔴 >100
"""
from __future__ import annotations

import datetime
from django.utils import timezone


def _ago(dt) -> tuple[str, int]:
    """Возвращает (текст_сколько_назад, секунд_назад)."""
    if not dt:
        return ('никогда', 10**9)
    sec = int((timezone.now() - dt).total_seconds())
    if sec < 60:
        return (f'{sec} сек', sec)
    if sec < 3600:
        return (f'{sec // 60} мин', sec)
    if sec < 86400:
        return (f'{sec // 3600} ч', sec)
    return (f'{sec // 86400} д', sec)


def _pick_emoji(sec: int, green_lt: int, yellow_lt: int) -> str:
    if sec < green_lt:
        return '🟢'
    if sec < yellow_lt:
        return '🟡'
    return '🔴'


def collect_status() -> str:
    """Возвращает Telegram-готовый markdown."""
    from cargo.models import (
        AltaInboxMessage, AltaOutboxObservation, DeklarantSession,
        ImportedSheetRow, Cargo, HouseWaybill,
    )
    lines = []
    lines.append(f'📊 *CargoTrack Status* — {timezone.localtime().strftime("%d.%m %H:%M")}')
    lines.append('')

    # Alta agent (inbox + outbox)
    last_in = AltaInboxMessage.objects.order_by('-received_at').values_list(
        'received_at', flat=True).first()
    last_out = AltaOutboxObservation.objects.order_by('-prepared_at').values_list(
        'prepared_at', flat=True).first()
    in_txt, in_sec = _ago(last_in)
    out_txt, out_sec = _ago(last_out)
    worst = max(in_sec, out_sec)
    lines.append(f'{_pick_emoji(worst, 300, 1800)} *Alta agent*')
    lines.append(f'   Inbox последний: {in_txt} назад')
    lines.append(f'   Outbox последний: {out_txt} назад')
    lines.append('')

    # Deklarant
    ds = DeklarantSession.objects.filter(is_active=True).order_by('-created_at').first()
    if ds:
        used_txt, used_sec = _ago(ds.last_used_at)
        emoji = _pick_emoji(used_sec, 86400, 86400 * 3)
        lines.append(f'{emoji} *Deklarant Plus*')
        lines.append(f'   Session id={ds.id} login={ds.login}')
        lines.append(f'   Last used: {used_txt} назад')
    else:
        lines.append(f'🔴 *Deklarant Plus* — нет активной сессии (нужен deklarant_login)')
    lines.append('')

    # Sheets import (last imported row across all sources)
    last_imp = ImportedSheetRow.objects.order_by('-last_imported_at').values_list(
        'last_imported_at', flat=True).first()
    imp_txt, imp_sec = _ago(last_imp)
    lines.append(f'{_pick_emoji(imp_sec, 300, 3600)} *Sheets import*')
    lines.append(f'   Последний row imported: {imp_txt} назад')
    lines.append('')

    # Orphan / Unapplied
    orph = AltaInboxMessage.objects.filter(
        status_applied=False, cargo__isnull=True, hawb__isnull=True).count()
    unap = AltaInboxMessage.objects.filter(status_applied=False).count()
    lines.append(f'{_pick_emoji(orph, 10, 100)} *Inbox orphan* (без match): {orph}')
    lines.append(f'{_pick_emoji(unap, 10, 100)} *Inbox unapplied*: {unap}')
    lines.append('')

    # Totals
    n_cargo = Cargo.objects.count()
    n_hawb = HouseWaybill.objects.count()
    lines.append(f'📦 Cargo: {n_cargo}  •  HAWB: {n_hawb}')

    return '\n'.join(lines)
