from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, HouseWaybill


class Command(BaseCommand):
    help = ('Подсчитать HAWB у которых есть CMN-released в inbox, но '
            'release_date пуст или customs_status != RELEASED.')

    def handle(self, *args, **opts):
        released_msgs = AltaInboxMessage.objects.filter(
            msg_kind='released').exclude(hawb__isnull=True)
        by_hawb: dict = {}
        for m in released_msgs.select_related('hawb'):
            existing = by_hawb.get(m.hawb_id)
            if not existing or (m.prepared_at and m.prepared_at > existing.prepared_at):
                by_hawb[m.hawb_id] = m
        self.stdout.write(f'HAWB c released-msg: {len(by_hawb)}')

        broken = []
        for hid, m in by_hawb.items():
            h = m.hawb
            if not h:
                continue
            if h.release_date is None or h.customs_status != 'RELEASED':
                broken.append((h, m))
        self.stdout.write(f'Сломанных (release_date=None или status!=RELEASED): '
                          f'{len(broken)}')
        for h, m in broken[:30]:
            self.stdout.write(
                f'  HAWB {h.hawb_number}  status={h.customs_status!r}  '
                f'release={h.release_date}  msg.prep={m.prepared_at}')
