from django.core.management.base import BaseCommand

from cargo.models import AltaOutboxObservation


class Command(BaseCommand):
    help = 'Дамп raw_xml outbox observation в файл.'

    def add_arguments(self, parser):
        parser.add_argument('--pk', type=int)
        parser.add_argument('--hawb', default='')
        parser.add_argument('out_path')

    def handle(self, *args, **opts):
        obs = None
        if opts['pk']:
            obs = AltaOutboxObservation.objects.get(pk=opts['pk'])
        elif opts['hawb']:
            for o in AltaOutboxObservation.objects.order_by('-prepared_at'):
                pm = o.parsed_meta or {}
                if opts['hawb'] in (pm.get('hawbs') or []):
                    obs = o
                    break
        if not obs:
            self.stdout.write('Не найдено')
            return
        raw = (obs.parsed_meta or {}).get('raw_xml') or ''
        if not raw:
            self.stdout.write(f'pk={obs.pk} {obs.msg_type} — нет raw_xml')
            return
        with open(opts['out_path'], 'w', encoding='utf-8') as f:
            f.write(raw)
        self.stdout.write(
            f'pk={obs.pk} {obs.msg_type} {obs.prepared_at} — '
            f'saved {len(raw)} chars → {opts["out_path"]}')
