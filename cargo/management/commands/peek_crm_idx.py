"""Дамп состояния CrmHawbIndex + DB для одной HAWB."""
from django.core.management.base import BaseCommand

from cargo.models import CrmHawbIndex, HouseWaybill
from cargo.services.alta.ed_status import compute_ed_status, compute_t_value


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='+')

    def handle(self, *args, **opts):
        for hn in opts['hawbs']:
            self.stdout.write(f'=== {hn} ===')
            h = HouseWaybill.objects.filter(hawb_number=hn).first()
            if h:
                ed = compute_ed_status(h)
                t = compute_t_value(h)
                self.stdout.write(
                    f'  DB: pk={h.pk} status={h.customs_status!r} '
                    f'decl={h.customs_declaration_number!r} '
                    f'release={h.release_date}')
                self.stdout.write(f'  DB ed_status: {ed!r}')
                self.stdout.write(f'  DB compute_t: {t}')
            else:
                self.stdout.write('  DB: not in DB')
            for e in CrmHawbIndex.objects.filter(hawb_number=hn):
                self.stdout.write(
                    f'  IDX: tab={e.tab_name} row={e.row_index} '
                    f'last_decl={e.last_decl!r} last_status={e.last_status!r} '
                    f'last_hidden={e.last_hidden} last_t={e.last_t}')
