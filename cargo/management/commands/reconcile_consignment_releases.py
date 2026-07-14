"""Sweeper: доприменяет per-HAWB выпуск из consignment-блоков к накладным,
которых не было/не было привязано к партии на момент применения сообщения.

ПРОБЛЕМА (найдено 13.07.2026): CMN.11350 несёт N consignment-блоков с
per-HAWB DecisionCode. `apply_consignment_decisions` применяет решение
только к HAWB, привязанным к Cargo сообщения НА МОМЕНТ apply. Накладные,
которые тогда были сиротами (mawb=NULL) или ещё не в партии, молча
пропускаются, а сообщение помечается `status_applied=True`. Итог — «выпуск
прописался только к 1 из N». Класс НЕ ловится [[redispatch_stuck_finals]]:
тот ищет `status_applied=False`, а тут флаг True (применилось к одной).
Наблюдали 784-84687890: 1 из 8 RELEASED, 7 висели customs_status='' при
том, что все 8 consignment-блоков = decision_code 10 (выпуск).

РЕШЕНИЕ: судим по СОДЕРЖИМОМУ consignments, а не по status_applied. Для
released-сообщений с consignments сверяем блок (decision_code=10) с
фактическим customs_status каждой названной HAWB и доприменяем расхождения
прямым lean bulk_update (customs_status=RELEASED + рег.ДТ + release_date).
Гарды: HAWB существует и однозначна; нет более свежего released/rejected/
withdrawn для неё (не даунгрейдим). Sheets догоняет audit_sheets_vs_db +
crm_sync (как у [[stuck-release-backlog]]).

    manage.py reconcile_consignment_releases                    # dry
    manage.py reconcile_consignment_releases --apply
    manage.py reconcile_consignment_releases --apply --days 45 --limit 300
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from cargo.models import AltaInboxMessage, HouseWaybill


class Command(BaseCommand):
    help = ('Доприменяет выпуск из consignment-блоков к пропущенным HAWB '
            '(класс «выпуск прописался к 1 из N»).')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--days', type=int, default=45,
                            help='Окно по prepared_at (0 = все)')
        parser.add_argument('--limit', type=int, default=0,
                            help='Сколько сообщений за прогон (0 = все)')

    def handle(self, *args, **opts):
        from cargo.services.alta.inbox import (_build_declaration_number,
                                               _retry_on_locked)

        qs = (AltaInboxMessage.objects
              .filter(msg_kind='released', cargo__isnull=False)
              .order_by('-prepared_at'))
        if opts['days']:
            since = timezone.now() - timedelta(days=opts['days'])
            qs = qs.filter(prepared_at__gte=since)
        if opts['limit']:
            qs = qs[:opts['limit']]

        plan = {}   # hawb_number -> (decl, event_dt, msg_prepared)
        scanned = 0
        for m in qs.iterator():
            pm = m.parsed_meta or {}
            cons = pm.get('consignments') or []
            if not cons:
                continue
            scanned += 1
            decl = _build_declaration_number(pm)
            for c in cons:
                if str(c.get('decision_code') or '').strip() != '10':
                    continue  # только выпуск; отказ/отзыв не трогаем
                event_dt = parse_datetime(c.get('decision_date') or '') or m.prepared_at
                for wb in (c.get('waybills') or []):
                    wb = str(wb).strip()
                    # позже пришедшее решение по этой HAWB имеет приоритет
                    prev = plan.get(wb)
                    if prev is None or (m.prepared_at and prev[2]
                                        and m.prepared_at > prev[2]):
                        plan[wb] = (decl, event_dt, m.prepared_at)

        if not plan:
            self.stdout.write(f'просмотрено released-сообщений с consignments: '
                              f'{scanned}; кандидатов нет')
            return

        nums = list(plan)
        hs = {h.hawb_number: h for h in HouseWaybill.objects
              .filter(hawb_number__in=nums)}
        # неоднозначные (>1 копия) исключаем
        from django.db.models import Count
        dup = set(HouseWaybill.objects.filter(hawb_number__in=nums)
                  .values('hawb_number').annotate(n=Count('id'))
                  .filter(n__gt=1).values_list('hawb_number', flat=True))

        to_update = []
        for wb, (decl, event_dt, mp) in plan.items():
            if wb in dup:
                continue
            h = hs.get(wb)
            if not h or h.customs_status == 'RELEASED':
                continue
            # не даунгрейдим: есть более свежее финальное решение по HAWB?
            newer = AltaInboxMessage.objects.filter(
                hawb=h, prepared_at__gt=mp,
                msg_kind__in=('released', 'rejected', 'withdrawn')).exists()
            if newer:
                continue
            h.customs_status = 'RELEASED'
            if decl and not (h.customs_declaration_number or '').strip():
                h.customs_declaration_number = decl
            if event_dt and not h.release_date:
                h.release_date = event_dt
            to_update.append(h)

        self.stdout.write(
            f'просмотрено {scanned} сообщений; недоприменённых выпусков: '
            f'{len(to_update)}')
        for h in to_update[:30]:
            self.stdout.write(f'  {h.hawb_number} → RELEASED '
                              f'decl={h.customs_declaration_number or "—"}')
        if not opts['apply']:
            if to_update:
                self.stdout.write('(dry-run — добавь --apply)')
            return
        if not to_update:
            return

        with transaction.atomic():
            _retry_on_locked(
                HouseWaybill.objects.bulk_update, to_update,
                ['customs_status', 'customs_declaration_number', 'release_date'],
                batch_size=100)
        self.stdout.write(self.style.SUCCESS(
            f'применено выпусков: {len(to_update)} '
            '(Sheets догонит audit_sheets_vs_db + crm_sync)'))
