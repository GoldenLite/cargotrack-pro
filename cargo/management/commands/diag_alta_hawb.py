"""Diagnostic: показывает все AltaInboxMessage для конкретной HAWB или Cargo.

Запуск:
    uv run python manage.py diag_alta_hawb 10243504297
    uv run python manage.py diag_alta_hawb --cargo 295-12345678
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, Cargo, HouseWaybill
from cargo.services.alta.inbox import _build_declaration_number


class Command(BaseCommand):
    help = 'Показать inbox-сообщения по HAWB или Cargo'

    def add_arguments(self, parser):
        parser.add_argument('hawb_number', nargs='?', default='',
                            help='Номер HAWB')
        parser.add_argument('--cargo', default='',
                            help='Номер партии (AWB/CMR) вместо HAWB')

    def handle(self, *args, **opts):
        hawb_num = (opts['hawb_number'] or '').strip()
        cargo_num = (opts['cargo'] or '').strip()

        hawb = None
        cargo = None
        if hawb_num:
            hawb = HouseWaybill.objects.filter(hawb_number__iexact=hawb_num).first()
            if not hawb:
                self.stdout.write(self.style.ERROR(f'HAWB {hawb_num!r} не найдена'))
                return
            cargo = hawb.mawb
        elif cargo_num:
            cargo = Cargo.objects.filter(awb_number__iexact=cargo_num).first()
            if not cargo:
                self.stdout.write(self.style.ERROR(f'Cargo {cargo_num!r} не найдена'))
                return
        else:
            self.stdout.write(self.style.ERROR('Укажи hawb_number или --cargo'))
            return

        # HAWB info
        if hawb:
            self.stdout.write(self.style.SUCCESS(f'\n=== HAWB {hawb.hawb_number} (id={hawb.pk}) ==='))
            self.stdout.write(f'  customs_declaration_number = {hawb.customs_declaration_number!r}')
            self.stdout.write(f'  customs_status             = {hawb.customs_status}')
            self.stdout.write(f'  logistics_status           = {hawb.logistics_status}')
            self.stdout.write(f'  mawb_id                    = {hawb.mawb_id}')

        # Cargo info + сёстры по партии
        if cargo:
            self.stdout.write(self.style.SUCCESS(f'\n=== Cargo {cargo.awb_number} (id={cargo.pk}) ==='))
            self.stdout.write(f'  customs_declaration_number = {cargo.customs_declaration_number!r}')
            self.stdout.write(f'  stage                      = {cargo.stage}')
            siblings = cargo.hawbs.all()
            self.stdout.write(f'  HAWB-ов в партии: {siblings.count()}')
            for h in siblings:
                self.stdout.write(f'    - {h.hawb_number} (id={h.pk})  '
                                  f'decl={h.customs_declaration_number!r}  '
                                  f'cs={h.customs_status}  ls={h.logistics_status}')

        # Inbox messages
        qs = AltaInboxMessage.objects.all()
        if hawb and cargo:
            qs = qs.filter(hawb=hawb) | qs.filter(cargo=cargo)
        elif hawb:
            qs = qs.filter(hawb=hawb)
        elif cargo:
            qs = qs.filter(cargo=cargo)
        qs = qs.distinct().order_by('prepared_at', 'received_at')

        self.stdout.write(self.style.SUCCESS(f'\n=== Inbox messages ({qs.count()}) ==='))
        if not qs.exists():
            self.stdout.write('  пусто')
            return

        self.stdout.write(f'  {"id":>6}  {"prepared_at":>19}  {"msg_type":<14}  {"kind":<11}  '
                          f'{"design":<6} {"decision":<8}  decl_built')
        for m in qs:
            meta = m.parsed_meta or {}
            built = _build_declaration_number(meta)
            ts = m.prepared_at.strftime('%Y-%m-%d %H:%M:%S') if m.prepared_at else '—'
            self.stdout.write(
                f'  {m.pk:>6}  {ts:>19}  {m.msg_type:<14}  {m.msg_kind:<11}  '
                f'{(meta.get("design_code") or ""):<6} '
                f'{(meta.get("decision_code") or ""):<8}  '
                f'{built}'
            )
            if meta.get('apply_error'):
                self.stdout.write(f'         apply_error: {meta["apply_error"]}')

        # Что recompute даст
        from cargo.services.alta.inbox import recompute_declaration
        self.stdout.write(self.style.WARNING('\n=== Что даст recompute (без записи): ==='))
        latest = qs.filter(msg_kind__in=('released', 'withdrawn'))\
                   .order_by('-prepared_at', '-received_at').first()
        if latest:
            built = _build_declaration_number(latest.parsed_meta or {})
            self.stdout.write(f'  Latest: msg #{latest.pk}  kind={latest.msg_kind}  '
                              f'prepared={latest.prepared_at}  decl_built={built!r}')
            if latest.msg_kind == 'withdrawn':
                self.stdout.write('  → стереть customs_declaration_number')
            else:
                self.stdout.write(f'  → записать {built!r}')
        else:
            self.stdout.write('  нет released/withdrawn — ничего бы не менялось')
