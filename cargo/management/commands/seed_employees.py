"""
Сидер тестовых сотрудников.

Создаёт пользователей-декларантов из реального списка ответственных по ТО.
"Лапшин Андрей" привязывается к существующему `andy` (только обновляются
first_name/last_name, чтобы не сбить пароль/суперюзера).

По умолчанию также распределяет существующие Cargo как декларант-ассигнменты
по сотрудникам (--no-assign — отключить).

Запуск:  python manage.py seed_employees
         python manage.py seed_employees --no-assign
         python manage.py seed_employees --password 12345
"""
import random
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from cargo.models import Cargo, CargoAssignment


# (last_name, first_name, username) — username должен быть уникален.
# 'andy' — существующий пользователь (Лапшин Андрей), его пароль и
# is_superuser не трогаем.
EMPLOYEES = [
    ('Азамов',     'Азам',       'azamov'),
    ('Беляева',    'Екатерина',  'belyaeva'),
    ('Горелова',   'Елена',      'gorelova'),
    ('Калина',     'Елена',      'kalina'),
    ('Коробкова',  'Екатерина',  'korobkova'),
    ('Краснова',   'Анастасия',  'krasnova'),
    ('Никонова',   'Светлана',   'nikonova'),
    ('Подолин',    'Алексей',    'podolin'),
    ('Пругар',     'Ольга',      'prugar'),
    ('Руднева',    'Мария',      'rudneva'),
    ('Шевченко',   'Анна',       'shevchenko'),
    ('Шушарина',   'Татьяна',    'shusharina'),
    ('Лапшин',     'Андрей',     'andy'),  # существующий аккаунт
]


class Command(BaseCommand):
    help = 'Создать тестовых сотрудников-декларантов и (опц.) назначить их на существующие Cargo'

    def add_arguments(self, parser):
        parser.add_argument(
            '--password', default='12345',
            help='Пароль для создаваемых пользователей (по умолчанию 12345)',
        )
        parser.add_argument(
            '--no-assign', action='store_true',
            help='Не создавать CargoAssignment для существующих партий',
        )
        parser.add_argument(
            '--clear-assignments', action='store_true',
            help='Перед созданием удалить все существующие CargoAssignment',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        password = options['password']
        do_assign = not options['no_assign']
        clear_assignments = options['clear_assignments']

        created, updated = self._upsert_users(password)
        self.stdout.write(
            f'  Создано: {created}, обновлено: {updated} '
            f'(пароль для новых: {password!r})'
        )

        if clear_assignments:
            n = CargoAssignment.objects.count()
            CargoAssignment.objects.all().delete()
            self.stdout.write(f'  Удалено старых назначений: {n}')

        if do_assign:
            assigned = self._assign_to_cargo()
            self.stdout.write(self.style.SUCCESS(
                f'  Создано назначений: {assigned}'
            ))
        else:
            self.stdout.write('  Назначения пропущены (--no-assign)')

        self.stdout.write(self.style.SUCCESS(
            f'\nГотово. Всего сотрудников из списка в БД: '
            f'{User.objects.filter(username__in=[u for _, _, u in EMPLOYEES]).count()}/{len(EMPLOYEES)}'
        ))

    # ─────────────────────────────────────────────────────────────────────
    def _upsert_users(self, password: str):
        created = 0
        updated = 0
        for last, first, username in EMPLOYEES:
            user, was_created = User.objects.get_or_create(
                username=username,
                defaults={
                    'first_name': first,
                    'last_name':  last,
                    'is_active':  True,
                    'is_staff':   True,
                },
            )
            if was_created:
                user.set_password(password)
                user.save(update_fields=['password'])
                created += 1
            else:
                # Не трогаем пароль/is_superuser (например, у andy)
                changed = []
                if user.first_name != first:
                    user.first_name = first
                    changed.append('first_name')
                if user.last_name != last:
                    user.last_name = last
                    changed.append('last_name')
                if not user.is_active:
                    user.is_active = True
                    changed.append('is_active')
                if not user.is_staff:
                    user.is_staff = True
                    changed.append('is_staff')
                if changed:
                    user.save(update_fields=changed)
                    updated += 1
        return created, updated

    def _assign_to_cargo(self) -> int:
        usernames = [u for _, _, u in EMPLOYEES]
        users = list(User.objects.filter(username__in=usernames))
        if not users:
            return 0

        cargos = list(Cargo.objects.all())
        if not cargos:
            self.stdout.write('  (Cargo не найдены — нечего назначать)')
            return 0

        rng = random.Random(42)  # детерминированно — удобно для тестов
        count = 0
        for cargo in cargos:
            user = rng.choice(users)
            _, was_created = CargoAssignment.objects.get_or_create(
                cargo=cargo, user=user, role='declarant',
                defaults={'is_active': True, 'note': 'seed_employees'},
            )
            if was_created:
                count += 1
        return count
