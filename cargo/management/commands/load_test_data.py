"""
Команда для загрузки тестовых данных
Запуск: python manage.py load_test_data
"""
import random
from datetime import date, timedelta
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from cargo.models import Cargo, Warehouse, Flight, StatusHistory, STAGE_CHOICES


AIRLINES = ['SU', 'S7', 'U6', 'EK', 'TK', 'FZ', 'QR', 'LH']
IATA_ORIGINS = ['FRA', 'AMS', 'DXB', 'IST', 'DOH', 'PVG', 'ICN', 'JFK', 'LHR', 'CDG']
IATA_DEST = ['SVO', 'DME', 'VKO', 'LED', 'AER', 'KZN']

DESCRIPTIONS = [
    ('Electronic components', 'Электронные компоненты'),
    ('Auto spare parts', 'Запчасти для автомобилей'),
    ('Medical equipment', 'Медицинское оборудование'),
    ('Clothing and accessories', 'Одежда и аксессуары'),
    ('Industrial machinery', 'Промышленное оборудование'),
    ('Cosmetics', 'Косметика'),
    ('Food supplements', 'Пищевые добавки'),
    ('Optical instruments', 'Оптические инструменты'),
    ('Software on media', 'Программное обеспечение'),
    ('Printed materials', 'Печатные материалы'),
]

STAGES = [s[0] for s in STAGE_CHOICES]
SHP_TYPES = ['IMPEX', 'B2C', 'B2B']


class Command(BaseCommand):
    help = 'Загрузка тестовых данных (склады, рейсы, грузы)'

    def add_arguments(self, parser):
        parser.add_argument('--count', type=int, default=50, help='Количество грузов')

    def handle(self, *args, **options):
        count = options['count']
        self.stdout.write('Создание тестовых данных...')

        # Получаем или создаём пользователя
        user, _ = User.objects.get_or_create(
            username='system',
            defaults={'first_name': 'Система', 'is_staff': True}
        )

        # Создаём склады
        warehouses = self._create_warehouses()
        self.stdout.write(f'  ✓ Складов: {len(warehouses)}')

        # Создаём рейсы
        flights = self._create_flights()
        self.stdout.write(f'  ✓ Рейсов: {len(flights)}')

        # Создаём грузы
        created = self._create_cargos(count, warehouses, flights, user)
        self.stdout.write(f'  ✓ Грузов создано: {created}')
        self.stdout.write(self.style.SUCCESS('Тестовые данные успешно загружены!'))

    def _create_warehouses(self):
        data = [
            ('ООО Шереметьево-Карго', 'СВХ/0001/2019/Ш', 'Москва', 'SVO'),
            ('ЗАО Домодедово-Карго', 'СВХ/0002/2018/Д', 'Москва', 'DME'),
            ('АО Внуково-Логистика', 'СВХ/0003/2020/В', 'Москва', 'VKO'),
            ('ООО Пулково-Терминал', 'СВХ/0004/2017/П', 'Санкт-Петербург', 'LED'),
        ]
        result = []
        for name, lic, city, iata in data:
            wh, _ = Warehouse.objects.get_or_create(
                license_number=lic,
                defaults={
                    'name': name, 'city': city, 'iata_code': iata,
                    'max_capacity_kg': random.uniform(5000, 50000),
                    'is_active': True,
                }
            )
            result.append(wh)
        return result

    def _create_flights(self):
        result = []
        today = date.today()
        for i in range(20):
            fdate = today - timedelta(days=random.randint(0, 30))
            airline = random.choice(AIRLINES)
            fn = f'{airline}{random.randint(100, 999)}'
            dep = random.choice(IATA_ORIGINS)
            arr = random.choice(IATA_DEST)
            f, _ = Flight.objects.get_or_create(
                flight_number=fn,
                flight_date=fdate,
                defaults={
                    'airline': airline,
                    'departure_iata': dep,
                    'arrival_iata': arr,
                }
            )
            result.append(f)
        return result

    def _create_cargos(self, count, warehouses, flights, user):
        created = 0
        existing_awbs = set(Cargo.objects.values_list('awb_number', flat=True))

        for i in range(count):
            awb = f'{random.randint(100, 999)}-{random.randint(10000000, 99999999)}'
            if awb in existing_awbs:
                continue

            flight = random.choice(flights)
            wh = random.choice(warehouses)
            desc_en, desc_ru = random.choice(DESCRIPTIONS)
            stage = random.choice(STAGES)

            pieces_decl = random.randint(1, 20)

            scan_in = timezone.now() - timedelta(days=random.randint(0, 14),
                                                  hours=random.randint(0, 23))
            scan_out = scan_in + timedelta(days=random.randint(1, 5)) if stage == 'RELEASED' else None

            cargo = Cargo(
                awb_number=awb,
                description=desc_en,
                description_ru=desc_ru,
                shp_type=random.choice(SHP_TYPES),
                stage=stage,
                flight_number=flight.flight_number,
                flight_date=flight.flight_date,
                departure_iata=flight.departure_iata,
                arrival_iata=flight.arrival_iata,
                weight=round(random.uniform(0.5, 500), 2),
                pieces_declared=pieces_decl,
                invoice_currency=random.choice(['USD', 'EUR', 'CNY']),
                invoice_value=round(random.uniform(10, 5000), 2),
                warehouse=wh,
                bond_location=f'A-{random.randint(1, 50)}-{random.randint(1, 20)}',
                scan_into_bond=scan_in,
                scan_out_of_bond=scan_out,
                created_by=user,
                last_status_change=timezone.now() - timedelta(hours=random.randint(1, 100)),
            )
            if stage == 'RELEASED':
                cargo.release_date = scan_out
            cargo.save()

            # Несколько записей в истории
            StatusHistory.objects.create(
                cargo=cargo,
                old_status='',
                new_status=stage,
                changed_by=user,
                comment='Тестовые данные',
            )
            existing_awbs.add(awb)
            created += 1

        return created
