"""
Команда для загрузки тестовых данных
Запуск: python manage.py load_test_data [--count 50]
"""
import random
from datetime import date, timedelta
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from cargo.models import Cargo, Warehouse, Flight, StatusHistory, HouseWaybill

AIRLINES = ['SU', 'S7', 'EK', 'TK', 'FZ', 'QR', 'LH', 'U6']
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
    ('Spare parts', 'Запасные части'),
    ('Textile goods', 'Текстильные товары'),
]

CONSIGNEES = [
    ('ООО Техно-Трейд', '7701234567'),
    ('ИП Иванов А.А.', '503456789012'),
    ('АО МедПоставка', '7723456789'),
    ('ООО Логистика Плюс', '7734567890'),
    ('ЗАО ИмпортМаш', '7745678901'),
    ('ООО Элит Груп', '7756789012'),
    ('ИП Петрова М.С.', '504567890123'),
    ('ООО СтройМат', '7767890123'),
    ('АО ФармИмпорт', '7778901234'),
    ('ООО ТрейдСервис', '7789012345'),
]

STATUSES_HAWB = ['CNPK', 'CIDM', 'CVAL', 'DUTY', 'HOLD', 'EXAM', 'RLSE', 'REJ', 'HLDP', 'NCI', 'MPWI']
CARGO_TYPES = ['B2C', 'B2B', 'C2C']
SHIPMENT_TYPES = ['IMPORT', 'EXPORT']


class Command(BaseCommand):
    help = 'Загрузка тестовых данных (склады, рейсы, грузы, HAWB)'

    def add_arguments(self, parser):
        parser.add_argument('--count', type=int, default=30, help='Количество MAWB')
        parser.add_argument('--hawb-per-mawb', type=int, default=5, help='HAWB на каждый MAWB')

    def handle(self, *args, **options):
        count = options['count']
        hawb_per = options['hawb_per_mawb']

        user, _ = User.objects.get_or_create(
            username='system',
            defaults={'first_name': 'Система', 'is_staff': True}
        )

        # Создаём дополнительных пользователей для назначений
        staff_users = self._create_staff(user)
        self.stdout.write(f'  ✓ Сотрудников: {len(staff_users)}')

        warehouses = self._create_warehouses()
        self.stdout.write(f'  ✓ Складов СВХ: {len(warehouses)}')

        flights = self._create_flights()
        self.stdout.write(f'  ✓ Рейсов: {len(flights)}')

        mawb_count = self._create_cargos(count, warehouses, flights, user)
        self.stdout.write(f'  ✓ Партий MAWB: {mawb_count}')

        hawb_count = self._create_hawbs(hawb_per, staff_users)
        self.stdout.write(f'  ✓ Накладных HAWB: {hawb_count}')

        self.stdout.write(self.style.SUCCESS('Тестовые данные успешно загружены!'))

    def _create_staff(self, system_user):
        staff_data = [
            ('declarant1', 'Андрей', 'Смирнов'),
            ('declarant2', 'Мария', 'Козлова'),
            ('broker1', 'Дмитрий', 'Петров'),
            ('manager1', 'Елена', 'Иванова'),
        ]
        users = [system_user]
        for username, first, last in staff_data:
            u, _ = User.objects.get_or_create(
                username=username,
                defaults={'first_name': first, 'last_name': last, 'is_active': True}
            )
            users.append(u)
        return users

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
                defaults={'name': name, 'city': city, 'iata_code': iata,
                          'max_capacity_kg': random.uniform(5000, 50000), 'is_active': True}
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
                flight_number=fn, flight_date=fdate,
                defaults={'airline': airline, 'departure_iata': dep, 'arrival_iata': arr}
            )
            result.append(f)
        return result

    def _create_cargos(self, count, warehouses, flights, user):
        created = 0
        existing = set(Cargo.objects.values_list('awb_number', flat=True))
        stages = [s[0] for s in __import__('cargo.models', fromlist=['STAGE_CHOICES']).STAGE_CHOICES]

        for i in range(count):
            awb = f'{random.randint(100,999)}-{random.randint(10000000,99999999)}'
            if awb in existing:
                continue
            flight = random.choice(flights)
            wh = random.choice(warehouses)
            desc_en, desc_ru = random.choice(DESCRIPTIONS)
            stage = random.choice(stages)

            pieces_decl = random.randint(1, 20)
            scan_in = timezone.now() - timedelta(days=random.randint(0, 14), hours=random.randint(0, 23))
            scan_out = scan_in + timedelta(days=random.randint(1, 5)) if stage == 'RELEASED' else None

            cargo = Cargo(
                awb_number=awb, description=desc_en, description_ru=desc_ru,
                shp_type=random.choice(['IMPEX', 'B2C', 'B2B']),
                stage=stage,
                flight_number=flight.flight_number, flight_date=flight.flight_date,
                departure_iata=flight.departure_iata, arrival_iata=flight.arrival_iata,
                weight=round(random.uniform(0.5, 500), 2),
                pieces_declared=pieces_decl,
                invoice_currency=random.choice(['USD', 'EUR', 'CNY']),
                invoice_value=round(random.uniform(10, 5000), 2),
                warehouse=wh,
                bond_location=f'A-{random.randint(1,50)}-{random.randint(1,20)}',
                scan_into_bond=scan_in, scan_out_of_bond=scan_out,
                created_by=user,
                last_status_change=timezone.now() - timedelta(hours=random.randint(1, 200)),
            )
            if stage == 'RELEASED':
                cargo.release_date = scan_out
            cargo.save()
            StatusHistory.objects.create(
                cargo=cargo, old_status='', new_status=stage,
                changed_by=user, comment='Тестовые данные'
            )
            existing.add(awb)
            created += 1
        return created

    def _create_hawbs(self, hawb_per, staff_users):
        """Создаём HAWB для каждой партии с разными статусами и документами"""
        created = 0
        existing = set(HouseWaybill.objects.values_list('hawb_number', flat=True))
        cargos = list(Cargo.objects.all()[:30])

        for cargo in cargos:
            wh_license = cargo.warehouse_license or 'СВХ/0001/2019/Ш'
            n = random.randint(2, hawb_per)
            for j in range(n):
                hawb_num = f'H{cargo.awb_number[-6:]}-{j+1:02d}'
                if hawb_num in existing:
                    continue

                status = random.choice(STATUSES_HAWB)
                consignee, inn = random.choice(CONSIGNEES)
                desc_en, desc_ru = random.choice(DESCRIPTIONS)
                pieces_decl = random.randint(1, 5)

                # Время размещения на СВХ — берём от MAWB или генерируем
                if cargo.scan_into_bond:
                    svh_time = cargo.scan_into_bond + timedelta(hours=random.randint(1, 6))
                else:
                    svh_time = timezone.now() - timedelta(days=random.randint(0, 10))

                # Документы — разная степень готовности
                doc_scenario = random.choices(
                    ['none', 'partial', 'almost', 'full'],
                    weights=[20, 30, 25, 25]
                )[0]
                doc_invoice = doc_scenario in ('partial', 'almost', 'full')
                doc_packing = doc_scenario in ('almost', 'full')
                doc_permit  = doc_scenario == 'full'
                doc_tech    = doc_scenario in ('almost', 'full')

                assigned = random.choice(staff_users) if random.random() > 0.3 else None

                release = None
                if status == 'RLSE':
                    release = svh_time + timedelta(days=random.randint(2, 8))

                HouseWaybill.objects.create(
                    mawb=cargo,
                    hawb_number=hawb_num,
                    description=desc_ru,
                    cargo_type=random.choice(CARGO_TYPES),
                    shipment_type=random.choice(SHIPMENT_TYPES),
                    consignee_name=consignee,
                    consignee_inn=inn,
                    consignee_city=random.choice(['Москва', 'СПб', 'Казань', 'Екатеринбург']),
                    weight=round(random.uniform(0.2, 50), 2),
                    pieces_declared=pieces_decl,
                    invoice_value=round(random.uniform(10, 2000), 2),
                    invoice_currency=random.choice(['USD', 'EUR', 'CNY']),
                    status=status,
                    last_status_change=timezone.now() - timedelta(hours=random.randint(1, 120)),
                    assigned_to=assigned,
                    scan_into_bond=svh_time,
                    customs_declaration_number=f'10000/{svh_time.strftime("%d%m%y")}/{random.randint(1000000,9999999)}' if status in ('DUTY', 'RLSE', 'EXAM') else '',
                    release_date=release,
                    doc_invoice=doc_invoice,
                    doc_packing_list=doc_packing,
                    doc_permit=doc_permit,
                    doc_tech_desc=doc_tech,
                    notes='Тестовые данные',
                )
                existing.add(hawb_num)
                created += 1
        return created
