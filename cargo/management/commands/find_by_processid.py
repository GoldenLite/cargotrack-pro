"""Найти AltaInboxMessage с указанным ProcessID в raw_xml или parsed_meta."""
from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, AltaOutboxObservation


class Command(BaseCommand):
    help = 'Поиск сообщений по ProcessID (анкер цепочки в Альте)'

    def add_arguments(self, parser):
        parser.add_argument('process_id')

    def handle(self, *args, **opts):
        pid = opts['process_id'].strip().lower()
        self.stdout.write(self.style.NOTICE(f'ProcessID: {pid}'))

        self.stdout.write('\n--- AltaInboxMessage с этим ProcessID в raw_xml ---')
        for m in AltaInboxMessage.objects.filter(
                raw_xml__icontains=pid).order_by('prepared_at'):
            pm = m.parsed_meta or {}
            self.stdout.write(
                f'  pk={m.pk}  {m.prepared_at:%Y-%m-%d %H:%M:%S}  '
                f'{m.msg_type:12s} kind={m.msg_kind:14s}  '
                f'gtd={pm.get("gtd_number", ""):8s}  hawb_id={m.hawb_id}')

        self.stdout.write('\n--- AltaOutboxObservation с ProcessID ---')
        for o in AltaOutboxObservation.objects.all():
            pm = o.parsed_meta or {}
            raw = pm.get('raw_xml') or ''
            if pid in raw.lower():
                self.stdout.write(
                    f'  pk={o.pk}  {o.prepared_at:%Y-%m-%d %H:%M:%S}  '
                    f'{o.msg_type:10s}  hawbs={pm.get("hawbs", [])}')
