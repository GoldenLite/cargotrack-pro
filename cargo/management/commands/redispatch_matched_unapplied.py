"""Пере-диспатч застрявших unapplied ЭД-сообщений, ссылающихся на СУЩЕСТВУЮЩИЕ HAWB.

Гонка (особенно ЭКСПОРТ): таможенный выпуск (CMN.11341 «выпуск разрешён»)
приходит на СЕКУНДЫ раньше, чем наша исходящая подача (CMN.11335/11349)
успевает создать HAWB в БД. dispatch в тот момент match() не находит HAWB →
сохраняет сообщение status_applied=False. А поскольку у CMN.11341 пустой
initial_envelope, обычные re-dispatch-свиперы (они якорятся на initial_envelope)
это сообщение НЕ подхватывают. Итог: экспортная накладная висит FILED, хотя
по факту выпущена (кейс 10281457445 — HAWB создана через 50 сек ПОСЛЕ выпуска).

Здесь берём unapplied actionable-сообщения, чей `waybill_number_raw` совпадает
с СУЩЕСТВУЮЩИМ в БД HAWB, и пере-диспатчим — теперь match() находит HAWB и
применяет выпуск/статус (recompute_declaration сам разносит на siblings по ДТ).
Чужой трафик общего ЭД-канала Альты (waybill_number_raw не из нашей БД) не
трогаем вообще — он остаётся unapplied, как и должен.

Идемпотентно: применённые сообщения становятся status_applied=True и в выборку
больше не попадают. Foreign остаются unapplied (match() их не находит → no-op).

    manage.py redispatch_matched_unapplied            # dry-run (только показать)
    manage.py redispatch_matched_unapplied --apply
    manage.py redispatch_matched_unapplied --apply --kind released
"""
from __future__ import annotations

import time
from collections import Counter

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, HouseWaybill
from cargo.services.alta.inbox import dispatch

ACTIONABLE = ['released', 'registered', 'rejected', 'withdrawn',
              'registration_rejected', 'hold']

# Бюджет времени на шаг: он крутится в hourly-конвейере run_maintenance
# (ExecTimeLimit 50 мин). dispatch()→recompute_declaration делает LIKE-скан по
# гигантскому raw_xml AltaOutboxObservation (559MB) ПО КАЖДОМУ sibling ДТ —
# для ДТ с десятками накладных это минуты. Пачка «отравленных» unapplied
# сообщений (выпуск фактически применён, но status_applied завис из-за прошлого
# kill) при каждом прогоне заново гоняла эти сканы → >50 мин → kill → шаги
# ПОСЛЕ (arrival/audit/CRM-реконсайлы) не выполнялись (инцидент 24-25.08.2026).
# Бюджет проверяется ПЕРЕД взятием нового сообщения — одно сообщение всегда
# доигрывается целиком (не рвём транзакцию), остаток уходит в следующий прогон.
BUDGET_SEC = 900


class Command(BaseCommand):
    help = 'Пере-диспатч unapplied сообщений, ссылающихся на существующие HAWB (гонка).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Реально пере-диспатчить (без флага — dry-run)')
        parser.add_argument('--kind', default='',
                            help='Только этот msg_kind (по умолчанию все actionable)')
        parser.add_argument('--limit', type=int, default=0)

    def handle(self, *args, **opts):
        kinds = [opts['kind']] if opts['kind'] else ACTIONABLE

        # Скан лёгкий: только (id, waybill_number_raw), без raw_xml (он тяжёлый).
        rows = list(AltaInboxMessage.objects
                    .filter(status_applied=False, msg_kind__in=kinds)
                    .exclude(waybill_number_raw='')
                    .values_list('id', 'waybill_number_raw', 'msg_kind'))
        our = set(HouseWaybill.objects
                  .filter(hawb_number__in=[w for _, w, _ in rows])
                  .values_list('hawb_number', flat=True))
        matched = [(i, w, k) for i, w, k in rows if w in our]
        if opts['limit']:
            matched = matched[:opts['limit']]

        self.stdout.write(f'unapplied actionable с waybill: {len(rows)}; '
                          f'из них ссылаются на НАШИ HAWB: {len(matched)}')
        self.stdout.write('  by kind: ' + str(dict(Counter(k for _, _, k in matched))))
        if not opts['apply']:
            self.stdout.write('(dry-run — добавь --apply)')
            return
        if not matched:
            return

        # Подавляем Sheets writeback на время dispatch-цикла — в конце пишем
        # ОДИН раз, только затронутые HAWB (иначе каждый dispatch дёргает Sheets).
        from cargo.services.sheets.writeback import (
            begin_batch_writeback, end_batch_writeback,
        )
        # Гард «отравленных»: released-сообщение, чей HAWB уже RELEASED — выпуск
        # фактически применён (dispatch прошлого раза успел выставить
        # customs_status, но был убит до status_applied=True). Помечаем applied
        # БЕЗ тяжёлого recompute (raw_xml-скан). Дешёвый прегруз статусов одним
        # запросом. Пропагацию ДТ по siblings добьёт decl_propagation + audit.
        status_by_hn = dict(HouseWaybill.objects
                            .filter(hawb_number__in=[w for _, w, _ in matched])
                            .values_list('hawb_number', 'customs_status'))

        begin_batch_writeback()
        applied = 0
        errors = 0
        skipped_done = 0
        budget_hit = False
        affected_decls = set()
        affected_ids = set()
        started = time.monotonic()
        try:
            for i, w, k in matched:
                if k == 'released' and status_by_hn.get(w) == 'RELEASED':
                    AltaInboxMessage.objects.filter(
                        pk=i, status_applied=False).update(status_applied=True)
                    skipped_done += 1
                    continue
                if time.monotonic() - started > BUDGET_SEC:
                    budget_hit = True
                    self.stdout.write(self.style.WARNING(
                        f'  бюджет {BUDGET_SEC}с исчерпан — обработано '
                        f'{applied + skipped_done}/{len(matched)}, остаток на '
                        f'следующий прогон'))
                    break
                try:
                    m = AltaInboxMessage.objects.get(pk=i)
                    dispatch(m)
                    m.refresh_from_db()
                    if m.status_applied and m.hawb_id:
                        applied += 1
                        affected_ids.add(m.hawb_id)
                        if m.hawb and m.hawb.customs_declaration_number:
                            affected_decls.add(m.hawb.customs_declaration_number)
                except Exception as e:
                    errors += 1
                    if errors <= 10:
                        self.stdout.write(f'  ERR msg #{i} ({w}): {e}')
        finally:
            end_batch_writeback()

        if skipped_done:
            self.stdout.write(
                f'  уже-применённые (флаг завис) → помечено applied: {skipped_done}')

        # Затронутые HAWB + siblings по ДТ (recompute разнёс выпуск на всю ДТ).
        affected = {}
        for h in HouseWaybill.objects.filter(pk__in=affected_ids):
            affected[h.pk] = h
        if affected_decls:
            for h in HouseWaybill.objects.filter(
                    customs_declaration_number__in=affected_decls):
                affected[h.pk] = h
        affected = list(affected.values())
        exp = [h for h in affected if (h.shipment_type or 'IMPORT').upper() == 'EXPORT']
        imp = [h for h in affected if (h.shipment_type or 'IMPORT').upper() != 'EXPORT']
        self.stdout.write(f'applied={applied} errors={errors} '
                          f'затронуто HAWB={len(affected)} (export={len(exp)} import={len(imp)})')

        # Целевой writeback: экспорт → «Экспортная статистика», прочее → «Общее»/CRM.
        if exp:
            try:
                from cargo.services.alta.outbox import _writeback_export_hawbs
                _writeback_export_hawbs(exp)
                self.stdout.write(f'  export writeback: {len(exp)} HAWB')
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  export writeback FAILED: {e}'))
        if imp:
            try:
                from cargo.services.sheets.writeback import (
                    batch_write_declarations_for_hawbs,
                    batch_write_release_dates_for_hawbs,
                    batch_write_ed_status_for_hawbs,
                )
                from cargo.services.sheets.crm_realtime import (
                    batch_write_all_for_crm_hawbs,
                )
                batch_write_declarations_for_hawbs(imp)
                batch_write_release_dates_for_hawbs(imp)
                batch_write_ed_status_for_hawbs(imp)
                batch_write_all_for_crm_hawbs(imp)
                self.stdout.write(f'  general/crm writeback: {len(imp)} HAWB')
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  general writeback FAILED: {e}'))

        self.stdout.write(self.style.SUCCESS(
            f'Готово. applied={applied} skipped_done={skipped_done} '
            f'errors={errors} budget_hit={budget_hit}'))
