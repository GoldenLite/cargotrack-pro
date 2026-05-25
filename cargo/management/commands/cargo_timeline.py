"""Timeline партии: текущее состояние + все CMN/ED источники для аудита.

Показывает по каждому MAWB:
1. Текущие СВХ-поля Cargo (warehouse_license, scan_into_bond, svh_do1_reg_number).
2. Список HAWB в БД с привязкой.
3. AltaInboxMessage связанные с этой партией (CMN.13010/13029/13014 от таможни)
   с временами.
4. AltaOutboxObservation (ED.DO1 наш + CMN.11023/11349) с временами.
5. Авто-детект странностей: префикс vs лицензия, HAWB с чужой связкой и т.д.

Запуск:
    uv run python manage.py cargo_timeline 120526-2 555-80974025 784-69020652
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import (
    Cargo, HouseWaybill, AltaInboxMessage, AltaOutboxObservation,
)
from cargo.services.alta.inbox import OUR_WAREHOUSE_LICENSE
from cargo.services.external_warehouse.applier import MOSCOW_CARGO_PREFIXES


def _prefix(mawb: str) -> str:
    mawb = (mawb or '').strip()
    if len(mawb) >= 4 and mawb[3] == '-':
        return mawb[:3]
    return ''


class Command(BaseCommand):
    help = 'Timeline партии: текущее состояние + все источники данных'

    def add_arguments(self, parser):
        parser.add_argument('mawbs', nargs='+', help='Один или несколько MAWB')

    def handle(self, *args, **opts):
        for mawb in opts['mawbs']:
            self.stdout.write('')
            self.stdout.write(self.style.NOTICE('=' * 70))
            self.stdout.write(self.style.NOTICE(f'  MAWB: {mawb}'))
            self.stdout.write(self.style.NOTICE('=' * 70))
            self._dump_one(mawb)

    def _dump_one(self, mawb: str):
        c = Cargo.objects.filter(awb_number=mawb).first()
        if not c:
            self.stdout.write(self.style.WARNING(f'Cargo {mawb} НЕТ в БД'))
            # Но может быть AltaOutboxObservation/AltaInbox упоминают?
            self._dump_observations(mawb, c)
            return

        # 1. Cargo state
        self.stdout.write('Cargo:')
        self.stdout.write(f'  pk                 = {c.pk}')
        self.stdout.write(f'  stage              = {c.stage}')
        self.stdout.write(f'  warehouse_license  = {c.warehouse_license!r}')
        self.stdout.write(f'  scan_into_bond     = {c.scan_into_bond}')
        self.stdout.write(f'  svh_do1_reg_number = {c.svh_do1_reg_number!r}')
        self.stdout.write(f'  customs_decl       = {c.customs_declaration_number!r}')
        self.stdout.write(f'  HAWBs в БД         = {c.hawbs.count()}')

        # Авто-детект странностей
        pref = _prefix(mawb)
        is_mc_prefix = pref in MOSCOW_CARGO_PREFIXES
        is_our_lic = (c.warehouse_license or '').strip() == OUR_WAREHOUSE_LICENSE
        is_mc_lic = (c.warehouse_license or '').strip().startswith('10005/')

        if is_mc_prefix and is_our_lic:
            self.stdout.write(self.style.WARNING(
                f'  ⚠ Префикс {pref} обычно Москва-Карго, но лицензия НАША '
                f'(10001/...) — нестандартно. Партия физически у нас? '
                f'Или moscow-cargo парсер не отработал?'
            ))
        if not is_mc_prefix and is_mc_lic:
            self.stdout.write(self.style.ERROR(
                f'  ❌ Префикс {pref} НЕ Москва-Карго, но лицензия 10005/... '
                f'— БАГ. Откуда лицензия?'
            ))
        if is_mc_prefix and is_mc_lic:
            self.stdout.write(self.style.SUCCESS(
                f'  ✓ Префикс Москва-Карго + лицензия 10005/... — норма'
            ))
        if not is_mc_prefix and is_our_lic:
            self.stdout.write(self.style.SUCCESS(
                f'  ✓ Наш префикс + наша лицензия — норма'
            ))
        if not c.warehouse_license:
            self.stdout.write(self.style.WARNING(
                f'  ⚠ Лицензия пустая. ED.DO1 нашего склада не приходил '
                f'И moscow-cargo парсер не отработал.'
            ))

        # 2. HAWB партии
        hawbs = list(c.hawbs.order_by('hawb_number'))
        if hawbs:
            self.stdout.write('')
            self.stdout.write(f'HAWB ({len(hawbs)}):')
            for h in hawbs[:20]:
                marks = []
                if h.svh_do1_sent_at:
                    marks.append(f'sent_at={h.svh_do1_sent_at.date()}')
                if h.svh_do2_send_at:
                    marks.append(f'do2={h.svh_do2_send_at.date()}')
                if h.customs_declaration_number:
                    marks.append(f'ДТ={h.customs_declaration_number}')
                self.stdout.write(
                    f'  {h.hawb_number}: {h.customs_status or "(нет статуса)"}  '
                    f'{" ".join(marks)}'
                )
            if len(hawbs) > 20:
                self.stdout.write(f'  ... ещё {len(hawbs)-20}')

        # 3+4. Outbox + Inbox
        self._dump_observations(mawb, c)

    def _dump_observations(self, mawb: str, c=None):
        # Outbox (что от нашей Альты — ED.DO1, CMN.11023, CMN.11349)
        obs = list(AltaOutboxObservation.objects.filter(
            common_waybill_number=mawb
        ).order_by('prepared_at'))
        self.stdout.write('')
        self.stdout.write(f'AltaOutboxObservation для {mawb}: {len(obs)}')
        for o in obs:
            pm = o.parsed_meta or {}
            cert = pm.get('certificate_number') or ''
            hawb_cnt = len(pm.get('hawbs') or [])
            self.stdout.write(
                f'  #{o.pk} {o.msg_type:<12} prepared={o.prepared_at}  '
                f'received={o.received_at}  cert={cert!r}  hawbs={hawb_cnt}'
            )

        # Inbox связанные с этим Cargo (через FK или через raw_xml)
        if c:
            inbox = list(AltaInboxMessage.objects.filter(cargo=c)
                         .order_by('prepared_at'))
            self.stdout.write('')
            self.stdout.write(f'AltaInboxMessage cargo={c.pk}: {len(inbox)}')
            for m in inbox[:30]:
                pm = m.parsed_meta or {}
                lic = pm.get('svh_warehouse_license') or ''
                self.stdout.write(
                    f'  #{m.pk} {m.msg_type:<10} {m.msg_kind:<22} '
                    f'prepared={m.prepared_at}  lic={lic!r}'
                )
            if len(inbox) > 30:
                self.stdout.write(f'  ... ещё {len(inbox)-30}')

            # Inbox упоминающие MAWB в raw_xml но НЕ привязанные к этому Cargo
            xml_only = list(AltaInboxMessage.objects.filter(
                raw_xml__icontains=mawb,
            ).exclude(cargo=c).order_by('prepared_at'))
            if xml_only:
                self.stdout.write(f'\nINBOX упоминают MAWB в raw_xml но '
                                  f'НЕ привязаны к Cargo {c.pk}: {len(xml_only)}')
                for m in xml_only[:10]:
                    pm = m.parsed_meta or {}
                    lic = pm.get('svh_warehouse_license') or ''
                    self.stdout.write(
                        f'  #{m.pk} {m.msg_type} {m.msg_kind}  '
                        f'prepared={m.prepared_at}  cargo={m.cargo_id}  '
                        f'lic={lic!r}'
                    )
                if len(xml_only) > 10:
                    self.stdout.write(f'  ... ещё {len(xml_only)-10}')
