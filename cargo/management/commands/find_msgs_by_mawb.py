"""Диагностика: где сидят сообщения СВХ (CMN.13029 / CMN.13010) для MAWB-а.

Используется когда пользователь видит в Sheets, что веса/места проставлены
(CMN.13029 сработал), а даты/номера ДО1 отсутствуют (CMN.13010 либо не
прилетел, либо не сматчился time-эвристикой).

Запуск:
    python manage.py find_msgs_by_mawb 425-10390656 298-52164044
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import (AltaInboxMessage, AltaOutboxObservation, Cargo,
                          HouseWaybill)


class Command(BaseCommand):
    help = 'Найти CMN.13029/13010 для MAWB и понять почему ДО1 не сматчился'

    def add_arguments(self, parser):
        parser.add_argument('mawb', nargs='+')

    def handle(self, *args, **opts):
        for mawb in opts['mawb']:
            self.show(mawb)
            self.stdout.write('')

    def show(self, mawb: str) -> None:
        self.stdout.write(self.style.NOTICE(
            f'\n{"="*60}\n  MAWB: {mawb}\n{"="*60}'))

        c = Cargo.objects.filter(awb_number=mawb).first()
        if c:
            self.stdout.write(f'Cargo: pk={c.pk} awb={c.awb_number!r}')
            self.stdout.write(f'  svh_do1_reg_number: {c.svh_do1_reg_number!r}')
            self.stdout.write(f'  scan_into_bond:     {c.scan_into_bond}')
            self.stdout.write(f'  warehouse_license:  {c.warehouse_license!r}')
        else:
            self.stdout.write('Cargo: None')

        # CMN.13029 — представление (svh_placed)
        placed = list(AltaInboxMessage.objects.filter(
            msg_type='CMN.13029',
            raw_xml__icontains=mawb,
        ).order_by('-prepared_at')[:5])
        self.stdout.write(f'\nCMN.13029 (представление СВХ) matching MAWB: {len(placed)}')
        for m in placed:
            cargo_link = f'cargo_id={m.cargo_id}' if m.cargo_id else 'cargo=None'
            self.stdout.write(
                f'  {m.prepared_at} | env={m.envelope_id} | {cargo_link} | '
                f'doc_id={(m.parsed_meta or {}).get("svh_document_id")!r} | '
                f'lic={(m.parsed_meta or {}).get("svh_warehouse_license")!r}'
            )

        # CMN.13010 — регистрация ДО1
        cargo_pks = [m.cargo_id for m in placed if m.cargo_id]
        do1_by_link = []
        if cargo_pks:
            do1_by_link = list(AltaInboxMessage.objects.filter(
                msg_type='CMN.13010',
                cargo_id__in=cargo_pks,
            ).order_by('-prepared_at'))
        self.stdout.write(f'\nCMN.13010 уже привязанные к этому Cargo: {len(do1_by_link)}')
        for m in do1_by_link:
            self.stdout.write(
                f'  {m.prepared_at} | env={m.envelope_id} | '
                f'reg={(m.parsed_meta or {}).get("svh_do1_reg_number")!r}')

        # Кандидаты CMN.13010 — form='1' (РЕАЛЬНАЯ регистрация ДО1) нашей
        # лицензии без cargo, в окне после представления.
        if placed:
            from datetime import timedelta
            from cargo.services.alta.inbox import OUR_WAREHOUSE_LICENSE
            self.stdout.write(f'\nКандидаты CMN.13010 form=1 (cargo=None) '
                              f'в окне +7 дней от представлений:')
            for p in placed:
                if not p.prepared_at:
                    continue
                start = p.prepared_at
                end   = p.prepared_at + timedelta(days=7)
                cands = AltaInboxMessage.objects.filter(
                    msg_type='CMN.13010',
                    msg_kind='svh_do1_registered',
                    cargo__isnull=True,
                    prepared_at__gte=start,
                    prepared_at__lte=end,
                ).order_by('prepared_at')
                cands = list(cands)
                self.stdout.write(f'  для CMN.13029 от {p.prepared_at} '
                                  f'env={p.envelope_id} — {len(cands)} form=1 кандидатов')
                for m in cands:
                    pm = m.parsed_meta or {}
                    self.stdout.write(
                        f'    {m.prepared_at} | env={m.envelope_id} | '
                        f'reg={pm.get("svh_do1_reg_number")!r}')

        # На всякий случай — все 13010 с этим MAWB в raw_xml
        raw = list(AltaInboxMessage.objects.filter(
            msg_type='CMN.13010',
            raw_xml__icontains=mawb,
        ).order_by('-prepared_at')[:10])
        self.stdout.write(f'\nCMN.13010 с этим MAWB в raw_xml: {len(raw)}')
        for m in raw:
            pm = m.parsed_meta or {}
            self.stdout.write(
                f'  {m.prepared_at} | env={m.envelope_id} | cargo_id={m.cargo_id} | '
                f'reg={pm.get("svh_do1_reg_number")!r} | kind={m.msg_kind}')

        # Исходящие наблюдения
        outs = list(AltaOutboxObservation.objects.filter(
            common_waybill_number=mawb,
        ).order_by('-prepared_at')[:10])
        self.stdout.write(f'\nAltaOutboxObservation (исходящие из Альты): {len(outs)}')
        for o in outs:
            self.stdout.write(
                f'  {o.prepared_at} | {o.msg_type} | env={o.envelope_id} | '
                f'meta_keys={list((o.parsed_meta or {}).keys())}')
