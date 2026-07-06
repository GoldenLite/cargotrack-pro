"""Quick reparse export-outbox observations через свежий xml_extract.

Используется одноразово после фикса парсера (хотим перепрогнать
исторические CMN.11335/11349/11024, чтобы transport_doc подтянулся
из raw_xml на уже накопленных observations).

После reparse — чистим этот файл.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_datetime, parse_date

from cargo.models import AltaOutboxObservation


class Command(BaseCommand):
    help = 'Re-parse raw_xml on export AltaOutboxObservation и пере-apply.'

    def add_arguments(self, parser):
        parser.add_argument('--since', default='2026-05-28',
                            help='Дата (YYYY-MM-DD) — prepared_at >= этой даты.')
        parser.add_argument(
            '--types',
            default='CMN.11335,CMN.11349,CMN.11024',
            help='comma-separated msg_type list',
        )
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        from cargo.services.alta.outbox import _apply_export_outbox
        from cargo.services.alta.xml_extract import parse_raw_xml

        since_raw = opts['since']
        since_dt = parse_datetime(since_raw)
        if since_dt is None:
            d = parse_date(since_raw)
            if d is None:
                self.stderr.write(f'bad --since: {since_raw!r}')
                return
            since_dt = datetime.combine(
                d, datetime.min.time(), tzinfo=timezone.utc,
            )

        types = [t.strip() for t in opts['types'].split(',') if t.strip()]

        qs = AltaOutboxObservation.objects.filter(
            msg_type__in=types,
            prepared_at__gte=since_dt,
        ).order_by('prepared_at')
        if opts['limit']:
            qs = qs[:opts['limit']]
        n = qs.count()
        self.stdout.write(
            f'reparse: {n} observations  '
            f'(types={types}, since={since_dt.isoformat()})'
        )

        if opts['dry_run']:
            return

        ok = 0
        skipped_no_raw = 0
        err = 0
        for o in qs.iterator():
            try:
                pm = o.parsed_meta or {}
                raw = pm.get('raw_xml') or ''
                if not raw:
                    skipped_no_raw += 1
                    continue
                fresh = parse_raw_xml(raw)
                merged = {**pm, **fresh, 'raw_xml': raw}
                o.parsed_meta = merged
                o.save(update_fields=['parsed_meta'])
                _apply_export_outbox(o)
                ok += 1
            except Exception as e:
                err += 1
                if err < 20:
                    env = (o.envelope_id or '')[:8]
                    self.stdout.write(f'  ERR {env} ({o.msg_type}): {e}')

        self.stdout.write(self.style.SUCCESS(
            f'Done: ok={ok} skipped_no_raw={skipped_no_raw} err={err}'
        ))
