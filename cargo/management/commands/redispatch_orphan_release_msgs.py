"""Re-dispatch orphan release/registration inbox-сообщений.

После расширения match() шагом 5 (regex по raw_xml, см. inbox.py) старые
сообщения, до этого попавшие в БД как orphan (status_applied=False,
hawb_id=None), теперь могут сматчиться. По probe от 08.06.2026 — таких
~2304, из них ~12 содержат HAWB-номер из нашей БД.

Скрипт идемпотентен: dispatch() пишет HouseWaybill.goods_count / decl /
release_date только если изменилось (idempotent UPDATE). Sheets writeback
тоже идемпотент. Повторный запуск на уже-applied сообщении — no-op.

Использование:
    manage.py redispatch_orphan_release_msgs                   # dry-run, counts
    manage.py redispatch_orphan_release_msgs --apply           # реальный прогон
    manage.py redispatch_orphan_release_msgs --apply --limit 20
    manage.py redispatch_orphan_release_msgs --types CMN.11010 CMN.11337
"""
from __future__ import annotations

import time
import traceback

from django.core.management.base import BaseCommand
from django.db import OperationalError

from cargo.models import AltaInboxMessage
from cargo.services.alta.inbox import dispatch


ORPHAN_TYPES = ['CMN.11010', 'CMN.11309', 'CMN.11341',
                'CMN.11337', 'CMN.11001', 'CMN.11350']
CHUNK = 100
PAUSE_BETWEEN_CHUNKS = 1.0
LOCK_RETRY_BACKOFF = [1, 2, 4, 8, 16]


class Command(BaseCommand):
    help = 'Re-dispatch orphan release/registration inbox с обновлённым match()'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Реально вызвать dispatch (по умолчанию dry-run)')
        parser.add_argument('--limit', type=int, default=0,
                            help='Обработать не более N сообщений')
        parser.add_argument('--types', nargs='+', default=ORPHAN_TYPES)

    def handle(self, *args, **opts):
        apply = bool(opts.get('apply'))
        limit = int(opts.get('limit') or 0)
        types = list(opts.get('types') or ORPHAN_TYPES)

        qs = AltaInboxMessage.objects.filter(
            msg_type__in=types,
            status_applied=False,
            hawb__isnull=True,
        ).order_by('id')

        total = qs.count()
        self.stdout.write(
            f'Target: {total} orphan messages  types={types}  '
            f'apply={apply}  limit={limit}')

        # Распределение по типам — для sanity
        from django.db.models import Count
        by_type = {r['msg_type']: r['c'] for r in
                   qs.values('msg_type').annotate(c=Count('id'))}
        for t in types:
            self.stdout.write(f'  {t:12s} = {by_type.get(t, 0)}')

        if not apply:
            self.stdout.write(self.style.WARNING(
                '\nDRY-RUN. Запусти с --apply для реального прогона.'))
            return

        if total == 0:
            self.stdout.write('Нечего обрабатывать.')
            return

        ids = list(qs.values_list('id', flat=True))
        if limit:
            ids = ids[:limit]

        metrics = {'ok': 0, 'already_applied': 0, 'no_match': 0, 'exception': 0}

        for i in range(0, len(ids), CHUNK):
            batch = ids[i:i + CHUNK]
            for mid in batch:
                msg = AltaInboxMessage.objects.filter(pk=mid).first()
                if not msg:
                    continue
                if msg.status_applied:
                    metrics['already_applied'] += 1
                    continue
                # Retry на database locked (SQLite WAL contention).
                done = False
                for attempt, wait in enumerate(LOCK_RETRY_BACKOFF + [0]):
                    try:
                        dispatch(msg)
                        msg.refresh_from_db()
                        if msg.status_applied:
                            metrics['ok'] += 1
                        else:
                            metrics['no_match'] += 1
                        done = True
                        break
                    except OperationalError as e:
                        if 'locked' in str(e).lower() and wait:
                            time.sleep(wait)
                            continue
                        self.stderr.write(f'  id={mid} OperationalError: {e}')
                        metrics['exception'] += 1
                        done = True
                        break
                    except Exception as e:
                        self.stderr.write(
                            f'  id={mid} {type(e).__name__}: {e}')
                        traceback.print_exc()
                        metrics['exception'] += 1
                        done = True
                        break
                if not done:
                    metrics['exception'] += 1
            self.stdout.write(
                f'  chunk {i//CHUNK + 1}/{(len(ids)+CHUNK-1)//CHUNK}: {metrics}')
            time.sleep(PAUSE_BETWEEN_CHUNKS)

        self.stdout.write(self.style.SUCCESS(f'\nDone: {metrics}'))
