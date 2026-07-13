"""Sweeper: подтягивает застрявшие ВЫПУСКИ в CRM-вкладки.

Проблема (13.07.2026): выпуск применён в БД (customs_status=RELEASED,
compute_ed_status='Выпуск разрешен'), но в CRM-вкладке специалиста
last_status пустой/иной — «выпуск не подтянулся в CRM». Корень:
realtime CRM-writeback из dispatch — best-effort (падает под lock-
конкуренцией, non-fatal), а crm_sync_incremental не успевает обойти
все 12 вкладок за 10-мин дедлайн (CrmReindex/crm_sort сбрасывают
last_synced_at у ВСЕХ 5379 записей → anti-starvation-приоритет не
разделяет → последние вкладки застревают). Итог — редкие выпуски
висят в CRM без статуса.

Этот sweeper НАДЁЖНО их догоняет: берёт RELEASED-HAWB, у которых в
CrmHawbIndex last_status != 'Выпуск разрешен', подтверждает выпуск
свежим compute_ed_status и делает realtime CRM-writeback. Лёгкий —
работает только с выпущенными (не все 5379).

    manage.py reconcile_crm_releases              # dry
    manage.py reconcile_crm_releases --apply
"""
from django.core.management.base import BaseCommand

from cargo.models import HouseWaybill, CrmHawbIndex


class Command(BaseCommand):
    help = 'Подтягивает застрявшие выпуски в CRM-вкладки (realtime writeback).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **opts):
        from cargo.services.alta.ed_status import (compute_ed_status,
                                                   ed_status_batch)

        # выпущенные номера
        released = set(HouseWaybill.objects
                       .filter(customs_status='RELEASED')
                       .values_list('hawb_number', flat=True))
        # их записи в CRM, где last_status ещё не 'Выпуск разрешен'
        idx = (CrmHawbIndex.objects
               .filter(hawb_number__in=list(released))
               .exclude(last_status__contains='Выпуск разрешен'))
        cand_nums = list({e.hawb_number for e in idx})
        hawbs = {h.hawb_number: h for h in HouseWaybill.objects
                 .filter(hawb_number__in=cand_nums).select_related('mawb')}

        stuck = []
        seen = set()
        with ed_status_batch():
            for hn in cand_nums:
                h = hawbs.get(hn)
                if not h or hn in seen:
                    continue
                # подтверждаем реальный выпуск свежим compute
                if 'Выпуск разрешен' in (compute_ed_status(h) or ''):
                    seen.add(hn)
                    stuck.append(h)

        self.stdout.write(f'застрявших выпусков в CRM: {len(stuck)}')
        if not opts['apply']:
            for h in stuck[:25]:
                self.stdout.write(f'  {h.hawb_number}')
            if stuck:
                self.stdout.write('(dry-run — добавь --apply)')
            return
        if not stuck:
            return

        from cargo.services.sheets.crm_realtime import (
            batch_write_all_for_crm_hawbs)
        n = batch_write_all_for_crm_hawbs(stuck)
        self.stdout.write(self.style.SUCCESS(
            f'realtime CRM writeback для {len(stuck)} HAWB: {n}'))
