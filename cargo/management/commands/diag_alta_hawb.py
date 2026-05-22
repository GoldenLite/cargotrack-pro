"""Diagnostic: показывает все AltaInboxMessage по HAWB, Cargo или номеру ДТ.

Запуск:
    uv run python manage.py diag_alta_hawb 10243504297
    uv run python manage.py diag_alta_hawb --cargo 784-83269745
    uv run python manage.py diag_alta_hawb --decl 5048997   # ищет по фрагменту decl
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, Cargo, HouseWaybill
from cargo.services.alta.inbox import _build_declaration_number


class Command(BaseCommand):
    help = 'Показать inbox-сообщения по HAWB, Cargo или номеру ДТ'

    def add_arguments(self, parser):
        parser.add_argument('hawb_number', nargs='?', default='',
                            help='Номер HAWB')
        parser.add_argument('--cargo', default='',
                            help='Номер партии (AWB/CMR) вместо HAWB')
        parser.add_argument('--decl', default='',
                            help='Поиск сообщений где decl_built содержит фрагмент '
                                 '(например 5048997). Полезно: пришла ли вообще нужная ДТ.')

    def handle(self, *args, **opts):
        hawb_num = (opts['hawb_number'] or '').strip()
        cargo_num = (opts['cargo'] or '').strip()
        decl_frag = (opts['decl'] or '').strip()

        if decl_frag:
            self._search_by_decl(decl_frag)
            return

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
            self.stdout.write(self.style.ERROR('Укажи hawb_number / --cargo / --decl'))
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
                          f'{"design":<6} {"decision":<8}  {"hawb":<6}  {"cargo":<6}  decl_built')
        for m in qs:
            meta = m.parsed_meta or {}
            built = _build_declaration_number(meta)
            ts = m.prepared_at.strftime('%Y-%m-%d %H:%M:%S') if m.prepared_at else '—'
            self.stdout.write(
                f'  {m.pk:>6}  {ts:>19}  {m.msg_type:<14}  {m.msg_kind:<11}  '
                f'{(meta.get("design_code") or ""):<6} '
                f'{(meta.get("decision_code") or ""):<8}  '
                f'{str(m.hawb_id or ""):<6}  '
                f'{str(m.cargo_id or ""):<6}  '
                f'{built}'
            )
            if meta.get('apply_error'):
                self.stdout.write(f'         apply_error: {meta["apply_error"]}')

        # Что recompute даст ПО КОНКРЕТНОЙ HAWB — отражает реальную логику
        # recompute_declaration: msg.hawb=X ИЛИ (raw_xml содержит X.hawb_number AND msg.cargo=X.mawb).
        self.stdout.write(self.style.WARNING('\n=== Что даст recompute (без записи) ==='))
        if hawb:
            from django.db.models import Q
            cond = Q(hawb=hawb)
            if hawb.mawb_id and hawb.hawb_number:
                cond = cond | (Q(raw_xml__icontains=hawb.hawb_number) & Q(cargo=hawb.mawb))
            hawb_qs = AltaInboxMessage.objects.filter(
                cond, msg_kind__in=('released', 'withdrawn'))\
                .order_by('-prepared_at', '-received_at')
            latest = hawb_qs.first()
            if latest:
                built = _build_declaration_number(latest.parsed_meta or {})
                via = 'direct' if latest.hawb_id == hawb.pk else 'raw_xml+cargo'
                self.stdout.write(f'  По HAWB {hawb.hawb_number}: latest msg #{latest.pk} ({via})  '
                                  f'kind={latest.msg_kind}  prepared={latest.prepared_at}')
                if latest.msg_kind == 'withdrawn':
                    self.stdout.write('  → стереть customs_declaration_number')
                else:
                    self.stdout.write(f'  → записать {built!r}')
            else:
                self.stdout.write('  По HAWB: нет released/withdrawn → ничего не меняется')
        else:
            self.stdout.write('  --cargo без HAWB: recompute по Cargo больше не работает '
                              '(каждая HAWB имеет свою декларацию). Прогоняй diag по конкретной HAWB.')

    def _search_by_decl(self, fragment: str):
        """Найти сообщения где фрагмент встречается в parsed_meta ИЛИ raw_xml.

        Двойной поиск: сначала по структурным полям (быстро, как раньше), затем
        по raw_xml на случай если фрагмент лежит в поле, которое мы не парсим.
        """
        by_meta = set(AltaInboxMessage.objects.filter(
            parsed_meta__gtd_number__icontains=fragment).values_list('pk', flat=True))
        by_raw = set(AltaInboxMessage.objects.filter(
            raw_xml__icontains=fragment).values_list('pk', flat=True))
        all_ids = by_meta | by_raw

        self.stdout.write(self.style.SUCCESS(
            f'Поиск {fragment!r}: gtd_number={len(by_meta)}, raw_xml={len(by_raw)}, '
            f'итого {len(all_ids)} уникальных сообщений'))

        only_in_raw = by_raw - by_meta
        if only_in_raw:
            self.stdout.write(self.style.WARNING(
                f'  {len(only_in_raw)} сообщений с фрагментом ТОЛЬКО в raw_xml — '
                f'значит он лежит в неразобранном поле XML'))

        qs = AltaInboxMessage.objects.filter(pk__in=all_ids).order_by('prepared_at')
        for m in qs[:50]:
            meta = m.parsed_meta or {}
            built = _build_declaration_number(meta)
            ts = m.prepared_at.strftime('%Y-%m-%d %H:%M:%S') if m.prepared_at else '—'
            in_raw_only = '*' if m.pk in only_in_raw else ' '
            self.stdout.write(
                f' {in_raw_only}#{m.pk}  {ts}  {m.msg_type:<14}  kind={m.msg_kind:<11}  '
                f'design={meta.get("design_code","")}  decision={meta.get("decision_code","")}  '
                f'hawb={m.hawb_id}  cargo={m.cargo_id}  decl={built}  '
                f'waybill={m.waybill_number_raw!r}'
            )
        if not all_ids:
            self.stdout.write(self.style.WARNING(
                f'  Сообщений с этим фрагментом в БД НЕТ — значит ни одно .gz '
                f'с таким номером не прочитано/не залито на VPS.'))
