from django.core.management.base import BaseCommand

from cargo.models import AltaOutboxObservation


class Command(BaseCommand):
    help = 'Дамп raw_xml outbox observation в файл.'

    def add_arguments(self, parser):
        parser.add_argument('obs_pk', type=int)
        parser.add_argument('out_path')

    def handle(self, *args, **opts):
        obs = AltaOutboxObservation.objects.get(pk=opts['obs_pk'])
        raw = (obs.parsed_meta or {}).get('raw_xml') or ''
        if not raw:
            self.stdout.write('Нет raw_xml')
            return
        with open(opts['out_path'], 'w', encoding='utf-8') as f:
            f.write(raw)
        self.stdout.write(f'Saved {len(raw)} chars → {opts["out_path"]}')
