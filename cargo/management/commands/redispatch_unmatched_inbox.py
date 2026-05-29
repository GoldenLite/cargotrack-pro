"""Re-dispatch inbox-сообщений у которых hawb_id=None но kind не info.

Сценарий: CMN.11337/11001/11350 пришли от таможни до того как наш агент
успел зарегистрировать соответствующую outbox-наблюдение, либо до
последних фиксов в match(). После повторного dispatch — привязываются
к HAWB и пропагируют decl на siblings.

Сигналы writeback ПОДАВЛЕНЫ — финальный writeback запускается отдельно.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage


KINDS_TO_REDISPATCH = (
    'registered', 'released', 'rejected',
    'withdrawn', 'hold', 'examination',
)


class Command(BaseCommand):
    help = ('Re-dispatch unmatched inbox-сообщений '
            '(hawb_id=None для значимых kind).')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--kinds', nargs='+',
                            default=list(KINDS_TO_REDISPATCH))

    def handle(self, *args, **opts):
        from cargo.services.alta.inbox import dispatch
        from cargo.services.sheets.writeback import (
            begin_batch_writeback, end_batch_writeback,
        )

        qs = AltaInboxMessage.objects.filter(
            hawb_id__isnull=True,
            msg_kind__in=opts['kinds'],
        ).order_by('prepared_at')
        total = qs.count()
        self.stdout.write(f'Кандидатов: {total}')

        if opts['dry_run']:
            for m in qs[:50]:
                pm = m.parsed_meta or {}
                self.stdout.write(
                    f'  #{m.pk}  {m.msg_type}  kind={m.msg_kind!r}  '
                    f'gtd={pm.get("gtd_number")!r}  '
                    f'init_env={pm.get("initial_envelope")!r}')
            return

        begin_batch_writeback()
        ok, err, still_none = 0, 0, 0
        try:
            for m in qs:
                try:
                    dispatch(m)
                    m.refresh_from_db(fields=['hawb_id'])
                    if m.hawb_id is not None:
                        ok += 1
                    else:
                        still_none += 1
                except Exception as e:
                    err += 1
                    if err < 10:
                        self.stdout.write(f'  ERR #{m.pk}: {e}')
        finally:
            end_batch_writeback()
        self.stdout.write(self.style.SUCCESS(
            f'\nОбработано={ok}, не сматчилось={still_none}, ERR={err}'))
