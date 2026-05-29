from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, AltaOutboxObservation


class Command(BaseCommand):
    help = 'Найти inbox CMN.11337/11001 как ответ на исходящую CMN для HAWB.'

    def add_arguments(self, parser):
        parser.add_argument('hawb')

    def handle(self, *args, **opts):
        hn = opts['hawb']
        obs = None
        for o in AltaOutboxObservation.objects.order_by('-prepared_at'):
            hs = (o.parsed_meta or {}).get('hawbs') or []
            if hn in hs:
                obs = o
                break
        if not obs:
            self.stdout.write('outbox не найден')
            return
        self.stdout.write(
            f'outbox: #{obs.pk}  {obs.msg_type}  prep={obs.prepared_at}  '
            f'env={obs.envelope_id}')
        reply = AltaInboxMessage.objects.filter(
            parsed_meta__initial_envelope__iexact=obs.envelope_id)
        self.stdout.write(f'replies via initial_envelope: {reply.count()}')
        for m in reply[:10]:
            self.stdout.write(
                f'  #{m.pk}  {m.msg_type}  {m.prepared_at}  '
                f'kind={m.msg_kind!r}')
