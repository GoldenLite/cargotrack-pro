"""Сидер данных для тестирования планирования нагрузки.

Идемпотентно создаёт:
  * UserProfile для всех пользователей из cargo.management.commands.seed_employees
    (с разными часовыми поясами для проверки TZ-логики).
  * Дефолтные нормативы (если их ещё нет — миграция 0031 их создаёт, но если
    БД уже содержит частично — добиваем недостающее).

С флагом --demo: создаёт перегрузочный сценарий — фейковую партию (MAWB) с
flight_date=today и 25 HAWB на каждого из --overload-users сотрудников. Это
гарантирует отображение перегруза в виджетах.

Запуск:
    python manage.py seed_workload                # только профили + нормативы
    python manage.py seed_workload --demo         # + перегрузочный сценарий
    python manage.py seed_workload --demo --clear # снести демо-данные и пересоздать
"""
import datetime as _dt
import random

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from cargo.management.commands.seed_employees import EMPLOYEES
from cargo.models import (
    DEFAULT_WORK_SCHEDULE, Cargo, HouseWaybill, ProcessingNorm, UserProfile,
    WorkloadRebalanceLog,
)


# (username, timezone, schedule_override_or_None)
PROFILE_OVERRIDES = {
    # Двое сотрудников в других TZ — для проверки логики
    'podolin':   ('Asia/Vladivostok', None),
    'krasnova':  ('Europe/Kaliningrad', None),
    # Один с короткой сменой (ПН-ПТ 10-18) — для проверки разной capacity
    'shevchenko': ('Europe/Moscow', {
        'mon': [['10:00', '18:00']], 'tue': [['10:00', '18:00']],
        'wed': [['10:00', '18:00']], 'thu': [['10:00', '18:00']],
        'fri': [['10:00', '18:00']], 'sat': [], 'sun': [],
    }),
}

DEFAULT_NORMS_LIST = [
    ('IMPORT', 'B2C', 30),
    ('IMPORT', 'B2B', 90),
    ('IMPORT', 'C2C', 45),
    ('IMPORT', 'DOC', 15),
    ('EXPORT', 'B2C', 25),
    ('EXPORT', 'B2B', 75),
    ('EXPORT', 'C2C', 40),
    ('EXPORT', 'DOC', 10),
]

DEMO_AWB_PREFIX = 'WLDEMO-'   # для лёгкой очистки демо-партий
DEMO_HAWB_PREFIX = 'WLDH-'


class Command(BaseCommand):
    help = 'Создать UserProfile, нормативы и (опц.) демо-перегруз для виджетов нагрузки'

    def add_arguments(self, parser):
        parser.add_argument('--demo', action='store_true',
                            help='Создать перегрузочный сценарий')
        parser.add_argument('--clear', action='store_true',
                            help='Удалить ранее созданные демо-данные перед запуском')
        parser.add_argument('--overload-users', type=int, default=3,
                            help='Сколько сотрудников перегружены (по умолчанию 3)')
        parser.add_argument('--hawbs-per-user', type=int, default=25,
                            help='HAWB на каждого перегруженного (по умолчанию 25)')
        parser.add_argument('--spread', type=int, default=5,
                            help='На сколько рабочих дней рассыпать нагрузку (по умолчанию 5)')

    @transaction.atomic
    def handle(self, *args, **opt):
        if opt['clear']:
            self._clear_demo()

        n_norms = self._upsert_norms()
        n_prof_new, n_prof_upd = self._upsert_profiles()

        self.stdout.write(self.style.SUCCESS(
            f'Нормативы: {n_norms} активных. Профили: создано {n_prof_new}, обновлено {n_prof_upd}.'
        ))

        if opt['demo']:
            n_mawb, n_hawb = self._seed_demo(
                overload_users=opt['overload_users'],
                hawbs_per_user=opt['hawbs_per_user'],
                spread=opt['spread'],
            )
            self.stdout.write(self.style.SUCCESS(
                f'\nДемо-сценарий: создано MAWB={n_mawb}, HAWB={n_hawb}.'
            ))
            self.stdout.write(
                'Откройте дашборд → «Добавить виджет» → раздел «Команда»\n'
                'или зайдите в галерею и добавьте «Нагрузка команды», «Перегруженные».'
            )

    # ── Нормативы ─────────────────────────────────────────────────────────
    def _upsert_norms(self) -> int:
        for shipment_type, cargo_type, minutes in DEFAULT_NORMS_LIST:
            ProcessingNorm.objects.update_or_create(
                shipment_type=shipment_type,
                cargo_type=cargo_type,
                defaults={'minutes': minutes, 'is_active': True},
            )
        return ProcessingNorm.objects.filter(is_active=True).count()

    # ── Профили ───────────────────────────────────────────────────────────
    def _upsert_profiles(self) -> tuple[int, int]:
        usernames = [u for _, _, u in EMPLOYEES]
        users = list(User.objects.filter(username__in=usernames))
        created = 0
        updated = 0
        for u in users:
            tz, schedule_override = PROFILE_OVERRIDES.get(u.username,
                                                          ('Europe/Moscow', None))
            schedule = schedule_override or DEFAULT_WORK_SCHEDULE
            profile, was_created = UserProfile.objects.get_or_create(
                user=u,
                defaults={
                    'timezone': tz,
                    'is_active_op': True,
                    'work_schedule': schedule,
                },
            )
            if was_created:
                created += 1
                continue
            # Обновим, если что-то поменялось
            changed = []
            if profile.timezone != tz:
                profile.timezone = tz; changed.append('timezone')
            if not profile.is_active_op:
                profile.is_active_op = True; changed.append('is_active_op')
            if not profile.work_schedule:
                profile.work_schedule = schedule; changed.append('work_schedule')
            if changed:
                profile.save(update_fields=changed)
                updated += 1

        # Деактивируем технических/системных пользователей (не из EMPLOYEES) —
        # чтобы они не попадали в распределение нагрузки.
        deactivated = UserProfile.objects.exclude(
            user__username__in=usernames,
        ).filter(is_active_op=True).update(is_active_op=False)
        if deactivated:
            self.stdout.write(
                f'  Деактивировано системных пользователей в распределении: {deactivated}'
            )
        return created, updated

    # ── Демо-перегруз ─────────────────────────────────────────────────────
    def _clear_demo(self):
        n_hawb = HouseWaybill.objects.filter(hawb_number__startswith=DEMO_HAWB_PREFIX).count()
        n_mawb = Cargo.objects.filter(awb_number__startswith=DEMO_AWB_PREFIX).count()
        n_log = WorkloadRebalanceLog.objects.filter(
            hawb__hawb_number__startswith=DEMO_HAWB_PREFIX
        ).count()
        WorkloadRebalanceLog.objects.filter(
            hawb__hawb_number__startswith=DEMO_HAWB_PREFIX
        ).delete()
        HouseWaybill.objects.filter(hawb_number__startswith=DEMO_HAWB_PREFIX).delete()
        Cargo.objects.filter(awb_number__startswith=DEMO_AWB_PREFIX).delete()
        self.stdout.write(
            f'  Очищено: MAWB={n_mawb}, HAWB={n_hawb}, лог-записей={n_log}'
        )

    def _seed_demo(self, overload_users: int, hawbs_per_user: int,
                   spread: int) -> tuple[int, int]:
        usernames = [u for _, _, u in EMPLOYEES]
        users = list(User.objects.filter(username__in=usernames).order_by('username'))
        if len(users) < overload_users + 2:
            self.stdout.write(self.style.WARNING(
                f'  Недостаточно сотрудников: есть {len(users)}, нужно ≥{overload_users + 2}'
            ))
            return 0, 0

        rng = random.Random(2026)
        today = timezone.localdate()

        # Собираем N ближайших рабочих дней (ПН-ПТ), начиная с сегодня
        work_days: list[_dt.date] = []
        cursor = today
        for _ in range(spread * 3):  # запас на выходные
            if cursor.weekday() < 5:
                work_days.append(cursor)
                if len(work_days) >= spread:
                    break
            cursor = cursor + _dt.timedelta(days=1)
        if not work_days:
            self.stdout.write(self.style.WARNING('  Не нашлось рабочих дней'))
            return 0, 0

        weekday_ru = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
        self.stdout.write(
            '  Целевые рабочие дни: ' + ', '.join(
                f'{d.strftime("%d.%m")} ({weekday_ru[d.weekday()]})'
                for d in work_days
            )
        )

        # Перегруженные — первые N сотрудников; со свободным временем — следующие 2
        overloaded = users[:overload_users]
        free_slack = users[overload_users:overload_users + 2]

        # Распределение по типам — смещаем к B2B для большей нагрузки
        cargo_type_pool = (['B2B'] * 4) + (['B2C'] * 3) + (['C2C'] * 2) + (['DOC'] * 1)
        ship_pool = (['IMPORT'] * 3) + (['EXPORT'] * 1)

        n_hawb_created = 0
        n_mawb_created = 0
        seq = 1
        # Бизнес-валидация HouseWaybill.save() требует AT_ORIGIN_WH при первичной
        # привязке к MAWB — создаём в этом статусе, затем bulk update переводим
        # в AT_SVH через QuerySet.update (минует save() и валидацию).
        ids_to_relabel: list[int] = []

        for day_idx, target_day in enumerate(work_days):
            # Отдельный MAWB на каждый день — чтобы flight_date был разным
            mawb, was_created = Cargo.objects.get_or_create(
                awb_number=f'{DEMO_AWB_PREFIX}{target_day.isoformat()}',
                defaults={
                    'description': f'Демо-партия для проверки нагрузки на {target_day.isoformat()}',
                    'shp_type': 'IMPEX',
                    'stage': 'ARRIVED',
                    'flight_date': target_day,
                    'departure_date': target_day - _dt.timedelta(days=1),
                    'weight': 0,
                    'pieces_declared': 0,
                },
            )
            if was_created:
                n_mawb_created += 1

            # Чтобы дни не были одинаковыми, делаем небольшой разброс по нагрузке:
            # на первый день — самый сильный перегруз, дальше слабее.
            day_factor = max(0.6, 1.0 - day_idx * 0.1)  # 1.0, 0.9, 0.8, 0.7, 0.6
            n_overload = max(8, int(hawbs_per_user * day_factor))

            for user in overloaded:
                for _ in range(n_overload):
                    hawb = HouseWaybill.objects.create(
                        mawb=mawb,
                        hawb_number=f'{DEMO_HAWB_PREFIX}{user.username}-{seq:04d}',
                        cargo_type=rng.choice(cargo_type_pool),
                        shipment_type=rng.choice(ship_pool),
                        consignee_name=f'Получатель {seq}',
                        weight=rng.randint(5, 40),
                        pieces_declared=1,
                        logistics_status='AT_ORIGIN_WH',
                        assigned_to=user,
                    )
                    ids_to_relabel.append(hawb.id)
                    seq += 1
                    n_hawb_created += 1

            # Свободным — мало накладных (2-4 шт случайно)
            for user in free_slack:
                for _ in range(rng.randint(2, 4)):
                    hawb = HouseWaybill.objects.create(
                        mawb=mawb,
                        hawb_number=f'{DEMO_HAWB_PREFIX}{user.username}-{seq:04d}',
                        cargo_type='DOC',
                        shipment_type='IMPORT',
                        consignee_name=f'Получатель {seq}',
                        weight=rng.randint(5, 40),
                        pieces_declared=1,
                        logistics_status='AT_ORIGIN_WH',
                        assigned_to=user,
                    )
                    ids_to_relabel.append(hawb.id)
                    seq += 1
                    n_hawb_created += 1

        # Перевести демо-HAWB в AT_SVH (минуя save() — это уже не «новая привязка»).
        if ids_to_relabel:
            HouseWaybill.objects.filter(id__in=ids_to_relabel).update(
                logistics_status='AT_SVH',
                logistics_status_date=timezone.now(),
            )

        return n_mawb_created, n_hawb_created
