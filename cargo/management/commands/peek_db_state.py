"""
peek_db_state — снимок состояния БД для оценки покрытия live-агента.

Возвращает один JSON в stdout с полями:
  inbox_total, inbox_by_type (top-15), inbox_first_received_at, inbox_last_received_at,
  inbox_first_prepared_at, inbox_last_prepared_at,
  outbox_total, outbox_first_received_at, outbox_last_received_at,
  outbox_first_prepared_at, outbox_last_prepared_at,
  hawb_total, hawb_with_decl, hawb_with_release,
  hawb_customs_requests_total
"""
from __future__ import annotations

import json
from django.core.management.base import BaseCommand
from django.db.models import Count

from cargo.models import (
    AltaInboxMessage,
    AltaOutboxObservation,
    HouseWaybill,
    HawbCustomsRequest,
)


def _iso(dt):
    return dt.isoformat() if dt else None


class Command(BaseCommand):
    help = "Снимок counts/min/max по inbox/outbox/HAWB — JSON в stdout."

    def handle(self, *args, **opts):
        inbox_qs = AltaInboxMessage.objects.all()
        outbox_qs = AltaOutboxObservation.objects.all()
        hawb_qs = HouseWaybill.objects.all()

        inbox_total = inbox_qs.count()
        outbox_total = outbox_qs.count()
        hawb_total = hawb_qs.count()

        # top-15 msg_type для inbox
        inbox_by_type = list(
            inbox_qs.values('msg_type')
            .annotate(count=Count('id'))
            .order_by('-count')[:15]
        )

        # min/max received_at и prepared_at
        inbox_received = inbox_qs.order_by('received_at').values_list(
            'received_at', flat=True)
        inbox_first_received = inbox_qs.order_by('received_at').values_list(
            'received_at', flat=True).first()
        inbox_last_received = inbox_qs.order_by('-received_at').values_list(
            'received_at', flat=True).first()
        inbox_first_prepared = inbox_qs.exclude(prepared_at=None).order_by(
            'prepared_at').values_list('prepared_at', flat=True).first()
        inbox_last_prepared = inbox_qs.exclude(prepared_at=None).order_by(
            '-prepared_at').values_list('prepared_at', flat=True).first()

        outbox_first_received = outbox_qs.order_by('received_at').values_list(
            'received_at', flat=True).first()
        outbox_last_received = outbox_qs.order_by('-received_at').values_list(
            'received_at', flat=True).first()
        outbox_first_prepared = outbox_qs.exclude(prepared_at=None).order_by(
            'prepared_at').values_list('prepared_at', flat=True).first()
        outbox_last_prepared = outbox_qs.exclude(prepared_at=None).order_by(
            '-prepared_at').values_list('prepared_at', flat=True).first()

        hawb_with_decl = hawb_qs.exclude(customs_declaration_number='').count()
        hawb_with_release = hawb_qs.exclude(release_date=None).count()
        hawb_customs_requests_total = HawbCustomsRequest.objects.count()

        payload = {
            'inbox_total': inbox_total,
            'inbox_by_type': inbox_by_type,
            'inbox_first_received_at': _iso(inbox_first_received),
            'inbox_last_received_at': _iso(inbox_last_received),
            'inbox_first_prepared_at': _iso(inbox_first_prepared),
            'inbox_last_prepared_at': _iso(inbox_last_prepared),
            'outbox_total': outbox_total,
            'outbox_first_received_at': _iso(outbox_first_received),
            'outbox_last_received_at': _iso(outbox_last_received),
            'outbox_first_prepared_at': _iso(outbox_first_prepared),
            'outbox_last_prepared_at': _iso(outbox_last_prepared),
            'hawb_total': hawb_total,
            'hawb_with_decl': hawb_with_decl,
            'hawb_with_release': hawb_with_release,
            'hawb_customs_requests_total': hawb_customs_requests_total,
        }
        self.stdout.write('===PEEK_DB_STATE_JSON===')
        self.stdout.write(json.dumps(payload, ensure_ascii=False))
        self.stdout.write('===END===')
