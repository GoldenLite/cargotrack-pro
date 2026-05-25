"""Timeline накладной (HAWB): текущее состояние + все источники.

Запуск:
    uv run python manage.py hawb_timeline 10268309640 10269467300
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from cargo.models import (
    HouseWaybill, AltaInboxMessage, AltaOutboxObservation,
    HawbWorkflowEvent, ImportedSheetRow,
)


class Command(BaseCommand):
    help = 'Timeline накладной: текущее состояние + связанные сообщения'

    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='+', help='Один или несколько HAWB-номеров')

    def handle(self, *args, **opts):
        for hn in opts['hawbs']:
            self.stdout.write('')
            self.stdout.write(self.style.NOTICE('=' * 70))
            self.stdout.write(self.style.NOTICE(f'  HAWB: {hn}'))
            self.stdout.write(self.style.NOTICE('=' * 70))
            self._dump_one(hn)

    def _dump_one(self, hn: str):
        h = HouseWaybill.objects.filter(hawb_number=hn).first()
        if not h:
            self.stdout.write(self.style.WARNING(f'HAWB {hn}: НЕТ в БД'))
            return

        # 1. HAWB state
        self.stdout.write('HouseWaybill:')
        self.stdout.write(f'  pk                  = {h.pk}')
        self.stdout.write(f'  mawb_id             = {h.mawb_id}')
        self.stdout.write(f'  mawb                = {h.mawb.awb_number if h.mawb_id and h.mawb else None}')
        self.stdout.write(f'  logistics_status    = {h.logistics_status}')
        self.stdout.write(f'  customs_status      = {h.customs_status!r}')
        self.stdout.write(f'  customs_declaration = {h.customs_declaration_number!r}')
        self.stdout.write(f'  filed_date          = {h.filed_date}')
        self.stdout.write(f'  release_date        = {h.release_date}')
        self.stdout.write(f'  scan_into_bond      = {h.scan_into_bond}')
        self.stdout.write(f'  svh_do1_sent_at     = {h.svh_do1_sent_at}')
        self.stdout.write(f'  svh_do1_weight      = {h.svh_do1_gross_weight}')
        self.stdout.write(f'  svh_do1_places      = {h.svh_do1_place_count}')
        self.stdout.write(f'  svh_do2_send_at     = {h.svh_do2_send_at}')
        self.stdout.write(f'  weight              = {h.weight}')

        # 2. ImportedSheetRow
        r = ImportedSheetRow.objects.filter(
            source__kind='general', hawb_number_norm=hn
        ).first()
        if r:
            self.stdout.write('')
            self.stdout.write(f'Sheets row:')
            self.stdout.write(f'  row_idx       = {r.source_row_index}')
            self.stdout.write(f'  match_status  = {r.match_status}')
            self.stdout.write(f'  last_imported = {r.last_imported_at}')

        # 3. AltaInboxMessage привязанные напрямую к HAWB
        inbox_direct = list(AltaInboxMessage.objects.filter(hawb=h)
                            .order_by('prepared_at'))
        self.stdout.write('')
        self.stdout.write(f'AltaInboxMessage (hawb={h.pk}): {len(inbox_direct)}')
        for m in inbox_direct[:30]:
            self.stdout.write(
                f'  #{m.pk} {m.msg_type:<10} {m.msg_kind:<22} '
                f'prepared={m.prepared_at}'
            )
        if len(inbox_direct) > 30:
            self.stdout.write(f'  ... ещё {len(inbox_direct)-30}')

        # 4. Inbox упоминающие HAWB-номер в raw_xml но НЕ привязанные
        in_raw = list(AltaInboxMessage.objects.filter(
            raw_xml__icontains=hn,
        ).exclude(hawb=h).order_by('prepared_at'))
        if in_raw:
            self.stdout.write('')
            self.stdout.write(
                f'AltaInboxMessage упоминают HAWB в raw_xml но НЕ привязаны: {len(in_raw)}'
            )
            for m in in_raw[:15]:
                self.stdout.write(
                    f'  #{m.pk} {m.msg_type} {m.msg_kind}  '
                    f'prepared={m.prepared_at}  cargo={m.cargo_id} hawb={m.hawb_id}'
                )
            if len(in_raw) > 15:
                self.stdout.write(f'  ... ещё {len(in_raw)-15}')

        # 5. Outbox observations упоминающие HAWB
        obs_direct = list(AltaOutboxObservation.objects.filter(
            Q(waybill_number=hn) | Q(hawb=h)
        ).order_by('prepared_at'))
        if obs_direct:
            self.stdout.write('')
            self.stdout.write(f'AltaOutboxObservation прямые: {len(obs_direct)}')
            for o in obs_direct[:15]:
                self.stdout.write(
                    f'  #{o.pk} {o.msg_type:<12} prepared={o.prepared_at} '
                    f'common_wb={o.common_waybill_number}'
                )

        # 6. Outbox observations где HAWB в parsed_meta['hawbs'] (нужно сканить)
        obs_in_meta = []
        # Ограничение: только ED.DO1 имеют parsed_meta['hawbs']
        for o in AltaOutboxObservation.objects.filter(msg_type='ED.DO1'):
            hawbs_list = (o.parsed_meta or {}).get('hawbs') or []
            if hn in hawbs_list:
                obs_in_meta.append(o)
        if obs_in_meta:
            self.stdout.write('')
            self.stdout.write(
                f'AltaOutboxObservation где HAWB в ED.DO1.parsed_meta: {len(obs_in_meta)}'
            )
            for o in obs_in_meta[:10]:
                self.stdout.write(
                    f'  #{o.pk} ED.DO1 prepared={o.prepared_at} '
                    f'common_wb={o.common_waybill_number}'
                )

        # 7. HawbWorkflowEvent
        events = list(HawbWorkflowEvent.objects.filter(hawb=h)
                      .order_by('occurred_at'))
        if events:
            self.stdout.write('')
            self.stdout.write(f'HawbWorkflowEvent: {len(events)}')
            for e in events[:30]:
                self.stdout.write(
                    f'  {e.occurred_at}  {e.event_type:<25} {e.source}  {e.raw_value or ""}'
                )

        # 8. Странности
        warnings = []
        if h.customs_declaration_number and not h.mawb_id:
            warnings.append(
                f'⚠ есть customs_declaration_number={h.customs_declaration_number!r} '
                f'но mawb_id=None'
            )
        if h.svh_do1_sent_at and not h.mawb_id:
            warnings.append(
                f'⚠ есть svh_do1_sent_at={h.svh_do1_sent_at} но mawb_id=None'
            )
        if h.svh_do2_send_at and not h.mawb_id:
            warnings.append(
                f'⚠ есть svh_do2_send_at={h.svh_do2_send_at} но mawb_id=None'
            )
        if (h.filed_date and h.release_date
                and h.release_date < h.filed_date):
            warnings.append(
                f'⚠ release_date ({h.release_date}) < filed_date ({h.filed_date})'
            )
        if h.customs_status == 'RELEASED' and not h.customs_declaration_number:
            warnings.append('⚠ статус RELEASED но customs_declaration_number пуст')

        if warnings:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING('Странности:'))
            for w in warnings:
                self.stdout.write(f'  {w}')
