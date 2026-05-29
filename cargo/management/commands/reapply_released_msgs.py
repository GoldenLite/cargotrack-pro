"""Восстановить release_date для HAWB у которых есть released-msg в inbox
но release_date пуст / status != RELEASED.

Причина: reclassify_msg_type для CMN.11337/11001 (стали 'registered'=FILED)
прокатился ПОСЛЕ того как HAWB уже был RELEASED — change_customs_status(FILED)
снёс release_date.

Логика:
1. Подавить writeback-сигналы (ImportedSheetRow может быть стейл).
2. Для каждого сломанного HAWB найти latest released-msg → apply_msg.
3. После завершения caller отдельно делает resync + re-writeback.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage


class Command(BaseCommand):
    help = 'Reapply released-msg для HAWB у которых release_date стёрся.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--limit', type=int, default=0)

    def handle(self, *args, **opts):
        from cargo.services.alta.inbox import dispatch
        from cargo.services.sheets.writeback import (
            begin_batch_writeback, end_batch_writeback,
        )

        released_msgs = AltaInboxMessage.objects.filter(
            msg_kind='released').exclude(hawb__isnull=True)
        # Latest по prepared_at для каждого HAWB.
        by_hawb: dict = {}
        for m in released_msgs.select_related('hawb'):
            existing = by_hawb.get(m.hawb_id)
            if not existing or (m.prepared_at and
                                m.prepared_at > existing.prepared_at):
                by_hawb[m.hawb_id] = m

        broken = []
        for hid, m in by_hawb.items():
            h = m.hawb
            if not h:
                continue
            if h.release_date is None or h.customs_status != 'RELEASED':
                broken.append(m)

        self.stdout.write(f'Сломанных HAWB: {len(broken)}')

        if opts['limit']:
            broken = broken[:opts['limit']]
            self.stdout.write(f'  (обрабатываем {len(broken)} по --limit)')

        if opts['dry_run']:
            for m in broken[:30]:
                self.stdout.write(
                    f'  would reapply HAWB {m.hawb.hawb_number} '
                    f'from msg #{m.pk} {m.msg_type} {m.prepared_at}')
            return

        begin_batch_writeback()
        ok, err = 0, 0
        try:
            for m in broken:
                try:
                    dispatch(m)
                    ok += 1
                except Exception as e:
                    err += 1
                    if err < 10:
                        self.stdout.write(
                            f'  ERR HAWB {m.hawb.hawb_number}: {e}')
        finally:
            end_batch_writeback()

        self.stdout.write(self.style.SUCCESS(
            f'Reapply готов. OK={ok}, ERR={err}'))
