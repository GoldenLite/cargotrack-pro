"""Точечный re-apply для CMN.11350 сообщений у которых dispatch() не сработал.

Контекст: в БД есть несколько CMN.11350 (released, decision_code=10) у которых
status_applied=False, при этом cargo/hawb FK уже выставлены (видимо через
re-link sweeper после факта). При повторном dispatch() match() возвращает
(None, None) — нет initial_envelope, declaration_number не построен из
parsed_meta, waybill_number_raw пуст. В результате dispatch уходит в else и
ничего не делает.

Эта команда обходит match() и вызывает apply_consignment_decisions(msg, msg.cargo)
напрямую — на уже-выставленных FK. Никакой новой логики, переиспользуем штатный
путь обработки consignments. На выходе msg.status_applied=True.

Безопасность:
- Список MSG_IDS жёстко зашит. Не сканируем БД массово.
- DRY-RUN по умолчанию, --apply для реального запуска.
- Conflict-check: если у HAWB уже стоит customs_declaration_number,
  отличный от computed по этому сообщению → SKIP с алертом.
- transaction.atomic() per-msg.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from cargo.models import AltaInboxMessage
from cargo.services.alta.inbox import (
    apply_consignment_decisions,
    _build_declaration_number,
    emit_event,
)


MSG_IDS = [69257, 69279, 69272, 69299]


class Command(BaseCommand):
    help = (
        'Точечный re-apply 4 CMN.11350 msg которые залипли applied=False '
        '(подача шла не через CargoTrack, нет initial_envelope, match не находит).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Реально применять. Без флага — DRY-RUN.',
        )

    def handle(self, *args, **opts):
        do_apply = bool(opts.get('apply'))
        mode = self.style.SUCCESS('APPLY') if do_apply else self.style.WARNING('DRY-RUN')
        self.stdout.write(f'Mode: {mode}')
        self.stdout.write(f'Target msg ids: {MSG_IDS}\n')

        for mid in MSG_IDS:
            self.stdout.write('=' * 72)
            try:
                msg = AltaInboxMessage.objects.get(id=mid)
            except AltaInboxMessage.DoesNotExist:
                self.stdout.write(self.style.ERROR(f'msg {mid}: НЕ НАЙДЕНО'))
                continue

            pm = msg.parsed_meta or {}
            computed = _build_declaration_number(pm)
            cons = pm.get('consignments') or []
            waybills = [w for c in cons for w in (c.get('waybills') or [])]

            self.stdout.write(
                f'msg {mid}: type={msg.msg_type} kind={msg.msg_kind} '
                f'applied={msg.status_applied} '
                f'cargo_id={msg.cargo_id} hawb_id={msg.hawb_id}'
            )
            self.stdout.write(f'  computed_decl = {computed!r}')
            self.stdout.write(f'  waybills      = {waybills}')

            # ── Guards ──
            if msg.status_applied:
                self.stdout.write(self.style.WARNING(
                    '  SKIP: status_applied=True (уже применён)'))
                continue
            if not msg.cargo_id:
                self.stdout.write(self.style.ERROR(
                    '  SKIP: cargo FK не выставлен — match не найдёт партию'))
                continue
            if not computed:
                self.stdout.write(self.style.ERROR(
                    '  SKIP: не удалось собрать декларацию из parsed_meta'))
                continue
            if not waybills:
                self.stdout.write(self.style.ERROR(
                    '  SKIP: в consignments нет waybills'))
                continue

            # ── Conflict-check: ни одна HAWB из waybills не должна иметь
            # ОТЛИЧАЮЩУЮСЯ декларацию ──
            conflict = False
            for wn in waybills:
                h = msg.cargo.hawbs.filter(hawb_number__iexact=wn).first()
                if not h:
                    self.stdout.write(self.style.WARNING(
                        f'  note: HAWB {wn} не в партии {msg.cargo.awb_number} '
                        f'— skip silently (так делает и сам apply)'))
                    continue
                cur = (h.customs_declaration_number or '').strip()
                if cur and cur != computed:
                    self.stdout.write(self.style.ERROR(
                        f'  CONFLICT: HAWB {wn} в БД decl={cur!r}, '
                        f'computed={computed!r} — пропускаем всё сообщение'))
                    conflict = True
            if conflict:
                continue

            if not do_apply:
                self.stdout.write(self.style.SUCCESS(
                    f'  WOULD call apply_consignment_decisions(msg, cargo={msg.cargo_id})'))
                self.stdout.write(self.style.SUCCESS(
                    '  WOULD set msg.status_applied=True + emit_event'))
                continue

            # ── APPLY ──
            try:
                with transaction.atomic():
                    err = apply_consignment_decisions(msg, msg.cargo)
                    if err:
                        pm2 = msg.parsed_meta or {}
                        pm2['apply_error'] = err
                        msg.parsed_meta = pm2
                        msg.status_applied = False
                        msg.save(update_fields=['status_applied', 'parsed_meta'])
                        self.stdout.write(self.style.ERROR(
                            f'  apply returned err: {err}'))
                    else:
                        msg.status_applied = True
                        msg.save(update_fields=['status_applied'])
                        emit_event(msg, msg.cargo, msg.hawb)
                        self.stdout.write(self.style.SUCCESS(
                            '  ✓ APPLIED: msg.status_applied=True; emit_event'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  EXCEPTION: {e}'))
                import traceback
                self.stdout.write(traceback.format_exc())

        self.stdout.write('\nГотово.')
