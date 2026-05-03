"""
Комплексный сидер базы данных.
Удаляет старые партии/накладные и создаёт свежие тестовые данные
по всем этапам обработки грузов с корректными весами, статусами и товарными позициями.

Запуск:            python manage.py seed
С полной очисткой: python manage.py seed --clear
"""
import random
from datetime import date, timedelta
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from cargo.models import (
    Cargo, HouseWaybill, HAWBGood, Warehouse, StatusHistory, CargoAssignment,
)

# ── Справочники ───────────────────────────────────────────────────────────────
AIRLINES      = ['SU', 'EK', 'FZ', 'TK', 'QR', 'LH', 'CX', 'CA', 'S7', 'U6']
IATA_ORIGINS  = ['PVG', 'HKG', 'FRA', 'DXB', 'IST', 'ICN', 'AMS', 'CDG', 'JFK', 'LHR']
IATA_DEST     = ['SVO', 'DME', 'VKO', 'LED']
SHP_TYPES     = ['IMPEX', 'B2C', 'B2B']
CARGO_TYPES   = ['B2C', 'B2B', 'C2C']
CURRENCIES    = ['USD', 'EUR', 'CNY']

DESCRIPTIONS = [
    ('Electronic components',     'Электронные компоненты'),
    ('Auto spare parts',          'Запчасти для автомобилей'),
    ('Medical equipment',         'Медицинское оборудование'),
    ('Clothing and accessories',  'Одежда и аксессуары'),
    ('Industrial machinery',      'Промышленное оборудование'),
    ('Cosmetics and perfumery',   'Косметика и парфюмерия'),
    ('Food supplements',          'Пищевые добавки'),
    ('Optical instruments',       'Оптические инструменты'),
    ('Textiles',                  'Текстильные товары'),
    ('Chemical reagents',         'Химические реагенты'),
]

CONSIGNEES = [
    ('ООО Техно-Трейд',      '7701234567',    'Москва'),
    ('ИП Иванов А.А.',        '503456789012',  'Санкт-Петербург'),
    ('АО МедПоставка',        '7723456789',    'Москва'),
    ('ЗАО ИмпортМаш',         '7745678901',    'Екатеринбург'),
    ('ООО Элит Груп',         '7756789012',    'Казань'),
    ('ИП Петрова М.С.',       '504567890123',  'Новосибирск'),
    ('АО ФармИмпорт',         '7778901234',    'Москва'),
    ('ООО ТрейдСервис',       '7789012345',    'Краснодар'),
    ('ООО РусИмпорт',         '7761234567',    'Ростов-на-Дону'),
    ('ЗАО ТехноЛогис',        '7772345678',    'Нижний Новгород'),
    ('ООО СпортЛайф',         '7790123456',    'Москва'),
    ('ЗАО ЭлектроСистемы',    '7712345678',    'Самара'),
]

# ── Каталог товаров по категориям ─────────────────────────────────────────────
GOODS_CATALOG = {
    'Электроника и компоненты': [
        ('Микроконтроллер STM32F407',  '8542310000', 'STMicroelectronics', 'STMicro', 'STM32F407VGT6', 'MCU-407',   'шт',  Decimal('8.50'),   'USD'),
        ('Конденсатор электролитический 470мкФ', '8532210000', 'Nichicon', 'Nichicon', 'UVR1E471MPD', 'CAP-470',  'шт',  Decimal('0.35'),   'USD'),
        ('OLED-дисплей 0.96" SSD1306',  '8531200000', 'WaveShare',  'WaveShare', 'OLED-096-I2C', 'DSP-096',   'шт',  Decimal('3.20'),   'USD'),
        ('Модуль Wi-Fi ESP32-WROOM',    '8517629090', 'Espressif',  'Espressif', 'ESP32-WROOM-32E', 'WIFI-32E', 'шт',  Decimal('4.80'),   'USD'),
        ('Источник питания 24V 5A',     '8504401900', 'MeanWell',   'MeanWell',  'LRS-100-24',   'PSU-100-24', 'шт',  Decimal('22.00'),  'USD'),
        ('Кабель USB Type-C 1м',        '8544421900', 'Anker',      'Anker',     'A8167',         'CABLE-TC1',  'шт',  Decimal('6.90'),   'USD'),
        ('SSD-накопитель 256 ГБ',       '8471706000', 'Samsung',    'Samsung',   'MZ-77E250B',    'SSD-256',    'шт',  Decimal('35.00'),  'USD'),
        ('Оперативная память DDR4 8GB',  '8473302900', 'Kingston',   'Kingston',  'KVR26N19S8/8',  'RAM-8G',     'шт',  Decimal('28.00'),  'USD'),
    ],
    'Автозапчасти': [
        ('Фильтр масляный VW Golf',     '8421231100', 'Mann-Filter','Mann',      'W 712/95',      'OIL-VW',     'шт',  Decimal('12.50'),  'EUR'),
        ('Тормозные колодки передние',  '8708309700', 'Bosch',      'Bosch',     'BP1234',        'BRK-FRT',    'комп',Decimal('45.00'),  'EUR'),
        ('Ремень ГРМ Toyota',           '4010360090', 'Gates',      'Gates',     'T273',          'BELT-TYT',   'шт',  Decimal('18.00'),  'EUR'),
        ('Амортизатор передний KYB',    '8708801000', 'KYB',        'KYB',       '335808',        'SHCK-FRT',   'шт',  Decimal('55.00'),  'EUR'),
        ('Свечи зажигания NGK (4шт)',   '8511100000', 'NGK',        'NGK',       'BKR6E-11',      'SPARK-NGK',  'комп',Decimal('22.00'),  'EUR'),
        ('Воздушный фильтр MANN',       '8421391500', 'Mann-Filter','Mann',      'C 25 114/1',    'AIR-VW2',    'шт',  Decimal('9.80'),   'EUR'),
        ('Термостат охлаждения',        '8484100000', 'Wahler',     'Wahler',    '4107.87D',      'THRM-W',     'шт',  Decimal('14.50'),  'EUR'),
    ],
    'Медицинское оборудование': [
        ('Пульсоксиметр портативный',   '9018193900', 'Contec',     'Contec',    'CMS50D',        'OXY-50D',    'шт',  Decimal('28.00'),  'USD'),
        ('Тонометр автоматический',     '9018110000', 'Omron',      'Omron',     'HEM-7120',      'BP-7120',    'шт',  Decimal('42.00'),  'USD'),
        ('Глюкометр OneTouch',          '9027801900', 'OneTouch',   'J&J',       'OT-Verio',      'GLU-OTV',    'шт',  Decimal('35.00'),  'USD'),
        ('Маска медицинская FFP2 (50шт)','6307909800', 'Kimberly',  'Kimberly',  'N95-KMB',       'MASK-50',    'упак',Decimal('18.50'),  'USD'),
        ('Шприц 5мл стерильный (100шт)','9018319800', 'BD',         'Becton',    'BD-5ML-100',    'SYR-5ML',    'упак',Decimal('12.00'),  'USD'),
        ('Хирургические перчатки L (100шт)','9021900000','Ansell',  'Ansell',    'MICRO-OP-L',    'GLOVE-L',    'упак',Decimal('24.00'),  'USD'),
    ],
    'Одежда и текстиль': [
        ('Куртка зимняя мужская р.L',   '6201930000', 'Columbia',   'Columbia',  'EM2853-010',    'JACKET-L',   'шт',  Decimal('95.00'),  'USD'),
        ('Футболка хлопок (12 шт)',     '6109100000', 'Gildan',     'Gildan',    'G200-WHT-XL',   'TEE-12X',    'упак',Decimal('36.00'),  'USD'),
        ('Джинсы мужские 32/32',        '6203423390', 'Levi\'s',    'Levi Strauss','501-0101',    'JEAN-3232',  'шт',  Decimal('55.00'),  'USD'),
        ('Носки спортивные (6 пар)',     '6115950000', 'Nike',       'Nike',      'SX7673-010',    'SOX-6PR',    'упак',Decimal('22.00'),  'USD'),
        ('Ткань хлопчатобумажная 50м',  '5209420000', 'Текстиль',   'ТекстильПро','COT-50',       'FAB-50M',    'м',   Decimal('3.50'),   'CNY'),
        ('Шарф шерстяной',              '6117100000', 'Burberry',   'Burberry',  'BB-SCR-GRY',    'SCARF-G',    'шт',  Decimal('120.00'), 'USD'),
    ],
    'Косметика': [
        ('Крем для лица SPF50 50мл',    '3304990000', 'La Roche',   'L\'Oréal',  'LRP-UVMELT',    'CREAM-50',   'шт',  Decimal('28.00'),  'EUR'),
        ('Шампунь восстанавливающий 250мл','3305100000','Kerastase', 'L\'Oréal',  'KERA-REST-250', 'SHP-250',    'шт',  Decimal('22.00'),  'EUR'),
        ('Парфюм женский 50мл',         '3303000000', 'Chanel',     'Chanel',    'COCO-EDT-50',   'PERF-CO50',  'шт',  Decimal('85.00'),  'EUR'),
        ('Маска для волос 200мл',       '3305900000', 'Redken',     'L\'Oréal',  'RDK-EX-200',    'MASK-200',   'шт',  Decimal('18.50'),  'EUR'),
        ('Сыворотка витамин C 30мл',    '3304990000', 'Skinceuticals','L\'Oréal','SKC-CE-FERULIC','SRM-30',     'шт',  Decimal('48.00'),  'USD'),
    ],
    'Промышленное оборудование': [
        ('Шаговый двигатель NEMA17',    '8501100000', 'Leadshine',  'Leadshine', 'NEMA17-40',     'MTR-N17',    'шт',  Decimal('15.00'),  'USD'),
        ('Частотный преобразователь 2.2кВт','8504401500','ABB',    'ABB',       'ACS310-03E',    'VFD-22K',    'шт',  Decimal('320.00'), 'EUR'),
        ('Датчик давления 0-16 бар',    '9026200900', 'Wika',       'Wika',      'S-20',          'SENS-16B',   'шт',  Decimal('42.00'),  'EUR'),
        ('Пневмоцилиндр DN63 L100',     '8412310000', 'SMC',        'SMC',       'C85N63-100',    'CYL-63-100', 'шт',  Decimal('68.00'),  'EUR'),
        ('Соленоидный клапан 24V',      '8481209000', 'Parker',     'Parker',    'VE131-1/4',     'SOL-VE131',  'шт',  Decimal('55.00'),  'EUR'),
        ('ПЛК Siemens S7-1200',         '8537101900', 'Siemens',    'Siemens',   '6ES7214-1AG40', 'PLC-S7-12',  'шт',  Decimal('480.00'), 'EUR'),
    ],
    'Пищевые добавки': [
        ('Протеин сывороточный 1кг',    '3504001900', 'Optimum',    'Glanbia',   'ON-WP-1KG',     'WHEY-1K',    'шт',  Decimal('32.00'),  'USD'),
        ('Омега-3 рыбий жир 90 капс',   '3004901900', 'NOW Foods',  'NOW Foods', 'NOW-1798',      'OMG3-90',    'шт',  Decimal('18.00'),  'USD'),
        ('Витамин D3 2000 IU 120 капс', '2936291000', 'Solgar',     'Solgar',    'SLG-D3-2K',     'VD3-120',    'шт',  Decimal('22.00'),  'USD'),
        ('Магний B6 60 таб',            '3004901900', 'Evalar',     'Evalar',    'EVL-MGB6',      'MGB6-60',    'шт',  Decimal('8.50'),   'USD'),
    ],
    'Оптические инструменты': [
        ('Объектив Canon EF 50mm f/1.8','9002111900', 'Canon',      'Canon',     'EF50-18II',     'LENS-5018',  'шт',  Decimal('125.00'), 'USD'),
        ('Бинокль 10x50',               '9005100000', 'Nikon',      'Nikon',     'ACULON-A211',   'BINO-1050',  'шт',  Decimal('85.00'),  'USD'),
        ('Микроскоп лабораторный',      '9011200000', 'Olympus',    'Olympus',   'CX23LEDRFS1',   'MICR-CX23', 'шт',  Decimal('750.00'), 'EUR'),
        ('Лазерный дальномер 60м',      '9015800000', 'Bosch',      'Bosch',     'GLM60',         'RANGE-60M',  'шт',  Decimal('95.00'),  'EUR'),
    ],
    'Химические реагенты': [
        ('Изопропиловый спирт 99% 5л',  '2905122000', 'Химреактив', 'ХимТех',   'IPA-99-5L',     'IPA-5L',     'л',   Decimal('12.00'),  'USD'),
        ('Ацетон технический 5л',       '2914110000', 'Химреактив', 'ХимТех',   'ACT-TECH-5L',   'ACE-5L',     'л',   Decimal('8.00'),   'USD'),
        ('Эпоксидная смола 1кг',        '3907300000', 'Momentive',  'Momentive', 'EPIKURE-3370',  'EPX-1K',     'кг',  Decimal('28.00'),  'USD'),
        ('Флюс для пайки 250мл',        '3810100000', 'MG Chem',    'MG Chem',   '8341-250ML',    'FLUX-250',   'мл',  Decimal('15.00'),  'USD'),
    ],
}

# Дополнительная единица измерения (ДЕИ) — мапа по артикулу.
# Значение: (кол-во ДЕИ на 1 единицу основной), название ДЕИ.
# Например: упаковка из 12 футболок → 12 шт ДЕИ; 1 пара носков отдельно — 1 пар.
# Категории, где ДЕИ обязательна по ТН ВЭД: одежда, обувь, лампы, шприцы, перчатки.
GOODS_DEI = {
    'TEE-12X':    (12, 'шт'),    # Футболка хлопок (12 шт)
    'SOX-6PR':    (6,  'пар'),   # Носки спортивные (6 пар)
    'MASK-50':    (50, 'шт'),    # Маска медицинская FFP2 (50шт)
    'SYR-5ML':    (100, 'шт'),   # Шприц 5мл (100шт)
    'GLOVE-L':    (100, 'пар'),  # Перчатки (100шт)
    'SPARK-NGK':  (4,  'шт'),    # Свечи зажигания NGK (4шт)
    'JACKET-L':   (1,  'шт'),    # Куртка
    'JEAN-3232':  (1,  'шт'),    # Джинсы
    'SCARF-G':    (1,  'шт'),    # Шарф
}

# Статусы согласования + веса вероятности для разных этапов накладной
APPROVAL_WEIGHTS_BY_STAGE = {
    'DRAFT':       {'pending': 10, 'approved': 0, 'clarification': 0, 'rejected': 0},
    'FORMED':      {'pending': 7,  'approved': 2, 'clarification': 1, 'rejected': 0},
    'DISPATCHED':  {'pending': 5,  'approved': 3, 'clarification': 2, 'rejected': 0},
    'ARRIVED':     {'pending': 3,  'approved': 4, 'clarification': 2, 'rejected': 1},
    'CUSTOMS':     {'pending': 1,  'approved': 5, 'clarification': 2, 'rejected': 2},
    'RELEASED':    {'pending': 0,  'approved': 8, 'clarification': 1, 'rejected': 1},
}


# ── Таможенные сценарии для этапа CUSTOMS ─────────────────────────────────────
CUSTOMS_SCENARIOS = {
    'mixed': [
        'BROKER_CHECK', 'READY_TO_FILE', 'FILED', 'EXAMINATION', 'HOLD',
        'BROKER_CHECK', 'READY_TO_FILE',
    ],
    'mixed_with_released': [
        'RELEASED', 'RELEASED', 'FILED', 'BROKER_CHECK', 'EXAMINATION',
    ],
    'all_released': ['RELEASED'],
}

TD_REQUIRED_STATUSES = {'FILED', 'EXAMINATION', 'HOLD', 'RELEASED'}


def _uniq_awb():
    for _ in range(100):
        awb = f'{random.randint(100, 999)}-{random.randint(10000000, 99999999)}'
        if not Cargo.objects.filter(awb_number=awb).exists():
            return awb
    raise RuntimeError('Не удаётся сгенерировать уникальный AWB')


def _uniq_hawb(prefix: str, idx: int):
    base = f'H{prefix[-7:]}{idx:02d}'
    if not HouseWaybill.objects.filter(hawb_number=base).exists():
        return base
    for _ in range(50):
        h = f'H{prefix[-7:]}{idx:02d}{random.randint(0, 9)}'
        if not HouseWaybill.objects.filter(hawb_number=h).exists():
            return h
    raise RuntimeError(f'Не удаётся сгенерировать уникальный HAWB ({prefix})')


def _td(dt=None):
    dt = dt or timezone.now()
    return f'10{random.randint(100, 999)}/{dt.strftime("%d%m%y")}/{random.randint(1_000_000, 9_999_999)}'


def _weighted_choice(weights: dict):
    """Выбор ключа по весам {'key': weight, ...}"""
    keys  = list(weights.keys())
    wvals = list(weights.values())
    total = sum(wvals)
    if total == 0:
        return keys[0]
    r = random.uniform(0, total)
    cumul = 0
    for k, w in zip(keys, wvals):
        cumul += w
        if r <= cumul:
            return k
    return keys[-1]


class Command(BaseCommand):
    help = 'Очистить и пересоздать тестовые данные по всем этапам партий'

    def add_arguments(self, parser):
        parser.add_argument(
            '--clear', action='store_true',
            help='Удалить ВСЕ существующие партии и накладные перед созданием',
        )

    def handle(self, *args, **options):
        if options['clear']:
            self._clear_data()

        user = self._get_system_user()
        whs  = self._ensure_warehouses()
        self.stdout.write(f'  Складов: {len(whs)}')

        # ─── Описание партий ──────────────────────────────────────────────────
        batches = [
            # DRAFT — партии ещё в сборке
            ('DRAFT',      4,  None,    'AT_ORIGIN_WH',   None),
            ('DRAFT',      3,  None,    'AT_ORIGIN_WH',   None),

            # FORMED — накладные объединены в консоль
            ('FORMED',     5,  None,    'CONSOLIDATED',   None),
            ('FORMED',     4,  None,    'READY_TO_SHIP',  None),

            # DISPATCHED — груз летит
            ('DISPATCHED', 5,  None,    'IN_TRANSIT_EXP', None),
            ('DISPATCHED', 4,  None,    'ARRIVED_DEST',   None),

            # ARRIVED — груз на складе
            ('ARRIVED',    5,  whs[0],  'AT_SVH',         None),
            ('ARRIVED',    4,  whs[1],  'AT_SVH',         None),

            # CUSTOMS — таможенное оформление
            ('CUSTOMS',    7,  whs[0],  'IMPORT_CUSTOMS', 'mixed'),
            ('CUSTOMS',    5,  whs[2],  'IMPORT_CUSTOMS', 'mixed_with_released'),

            # RELEASED — выпущено
            ('RELEASED',   4,  whs[1],  'DELIVERED',      'all_released'),
        ]

        for args_tuple in batches:
            self._create_batch(*args_tuple, user=user)

        sa_count = self._create_standalone_hawbs(user)

        goods_total = HAWBGood.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f'\nГотово!\n'
            f'  Партий:          {Cargo.objects.count()}\n'
            f'  HAWB в партиях:  {HouseWaybill.objects.filter(mawb__isnull=False).count()}\n'
            f'  Standalone HAWB: {sa_count}\n'
            f'  HAWB итого:      {HouseWaybill.objects.count()}\n'
            f'  Товарных позиций:{goods_total}\n'
        ))

    # ─────────────────────────────────────────────────────────────────────────

    def _clear_data(self):
        h = HouseWaybill.objects.count()
        c = Cargo.objects.count()
        HouseWaybill.objects.all().delete()
        Cargo.objects.all().delete()
        self.stdout.write(f'  Удалено: {c} партий, {h} накладных')

    def _get_system_user(self):
        user, _ = User.objects.get_or_create(
            username='system',
            defaults={'first_name': 'Система', 'is_staff': True},
        )
        return user

    def _ensure_warehouses(self):
        data = [
            ('ООО Шереметьево-Карго',  'СВХ/0001/2019/Ш', 'Москва',           'SVO', 50_000),
            ('ЗАО Домодедово-Карго',   'СВХ/0002/2018/Д', 'Москва',           'DME', 35_000),
            ('АО Внуково-Логистика',   'СВХ/0003/2020/В', 'Москва',           'VKO', 20_000),
            ('ООО Пулково-Терминал',   'СВХ/0004/2017/П', 'Санкт-Петербург',  'LED', 15_000),
        ]
        result = []
        for name, lic, city, iata, cap in data:
            wh, _ = Warehouse.objects.get_or_create(
                license_number=lic,
                defaults={
                    'name': name, 'city': city, 'iata_code': iata,
                    'max_capacity_kg': cap, 'is_active': True,
                },
            )
            result.append(wh)
        return result

    # ─────────────────────────────────────────────────────────────────────────
    def _create_batch(self, stage, hawb_count, warehouse, hawb_final_ls,
                      customs_scenario, user):
        awb        = _uniq_awb()
        desc_en, desc_ru = random.choice(DESCRIPTIONS)
        dep        = random.choice(IATA_ORIGINS)
        arr        = random.choice(IATA_DEST)
        flight_num = f'{random.choice(AIRLINES)}{random.randint(100, 999)}'

        days_map = {
            'DRAFT': None, 'FORMED': None, 'DISPATCHED': 6,
            'ARRIVED': 10, 'CUSTOMS': 14, 'RELEASED': 21,
        }
        days_ago_arrival = days_map.get(stage)
        flight_date = (date.today() - timedelta(days=days_ago_arrival)) if days_ago_arrival else None
        departure_date_val = (flight_date - timedelta(days=2)) if flight_date else None

        days_ago = {
            'DRAFT': 1, 'FORMED': 3, 'DISPATCHED': 6,
            'ARRIVED': 10, 'CUSTOMS': 14, 'RELEASED': 21,
        }.get(stage, 5)

        cargo = Cargo(
            awb_number      = awb,
            description     = desc_en,
            description_ru  = desc_ru,
            shp_type        = random.choice(SHP_TYPES),
            stage           = 'DRAFT',
            is_draft        = True,
            flight_number   = flight_num,
            departure_date  = None,
            flight_date     = flight_date,
            departure_iata  = dep,
            arrival_iata    = arr,
            transportation_mode = 4,
            pieces_declared = hawb_count * random.randint(1, 3),
            invoice_currency= random.choice(CURRENCIES),
            invoice_value   = Decimal(str(round(random.uniform(500, 8000), 2))),
            last_status_change  = timezone.now() - timedelta(days=days_ago),
            stage_changed_at    = timezone.now() - timedelta(days=days_ago),
            created_by      = user,
        )
        cargo.save()

        hawbs = []
        for j in range(hawb_count):
            consignee, inn, city = random.choice(CONSIGNEES)
            w   = Decimal(str(round(random.uniform(3.0, 70.0), 2)))
            pcs = random.randint(1, 5)
            h = HouseWaybill(
                mawb             = cargo,
                hawb_number      = _uniq_hawb(awb.replace('-', ''), j + 1),
                description      = desc_ru,
                cargo_type       = random.choice(CARGO_TYPES),
                shipment_type    = 'IMPORT',
                consignee_name   = consignee,
                consignee_inn    = inn,
                consignee_city   = city,
                weight           = w,
                pieces_declared  = pcs,
                invoice_value    = Decimal(str(round(random.uniform(50, 2000), 2))),
                invoice_currency = random.choice(CURRENCIES),
                logistics_status = 'AT_ORIGIN_WH',
                doc_invoice      = True,
                doc_packing_list = True,
                doc_permit       = True,
                doc_tech_desc    = True,
                docs_required    = 4,
                last_status_change = timezone.now() - timedelta(days=days_ago),
            )
            h.save()
            # Создаём товарные позиции
            self._create_goods(h, stage)
            hawbs.append(h)

        total_w = sum(float(h.weight) for h in hawbs)
        cargo.weight = Decimal(str(round(total_w, 2)))
        cargo.save()

        self._advance(cargo, hawbs, stage, hawb_final_ls, customs_scenario,
                      days_ago, warehouse)

        self.stdout.write(
            f'  [{stage:10s}]  {awb}  '
            f'{hawb_count} HAWB  {total_w:.1f} кг'
        )

    # ─────────────────────────────────────────────────────────────────────────
    def _create_goods(self, hawb: HouseWaybill, stage: str):
        """Создаёт 1–4 товарные позиции для накладной с реальными данными."""
        weights = APPROVAL_WEIGHTS_BY_STAGE.get(stage, APPROVAL_WEIGHTS_BY_STAGE['DRAFT'])

        # Выбираем случайную категорию, берём 1–4 позиции из неё
        category = random.choice(list(GOODS_CATALOG.keys()))
        items    = random.sample(GOODS_CATALOG[category], k=min(random.randint(1, 4), len(GOODS_CATALOG[category])))

        goods_to_create = []
        for name, tnved, brand, mfr, model, article, unit, unit_price, cur in items:
            qty        = Decimal(str(random.randint(1, 50)))
            net_unit   = Decimal(str(round(random.uniform(0.05, 5.0), 3)))
            gross_unit = net_unit * Decimal('1.15')
            total      = (unit_price * qty).quantize(Decimal('0.01'))

            # ДЕИ: если для артикула задана упаковочная норма — считаем доп. количество
            dei_per_unit, dei_unit = GOODS_DEI.get(article, (None, ''))
            if dei_per_unit is not None:
                quantity_additional = (qty * Decimal(str(dei_per_unit))).quantize(Decimal('0.001'))
                unit_additional = dei_unit
            else:
                quantity_additional = None
                unit_additional = ''

            # Для B2C — обязательна ссылка на товар. Генерируем фейковый URL карточки
            if hawb.cargo_type == 'B2C':
                slug = (article or 'p').lower().replace('/', '-').replace(' ', '-')
                product_url = f'https://shop.example.com/p/{slug}-{random.randint(1000, 9999)}'
            else:
                product_url = ''

            approval   = _weighted_choice(weights)
            comment    = ''
            approved_by = None
            approved_at = None

            if approval == 'clarification':
                comments = [
                    'Уточните код ТН ВЭД — возможно несоответствие категории',
                    'Требуется сертификат соответствия на данный товар',
                    'Укажите точное наименование производителя на русском',
                    'Необходимо предоставить техническое описание',
                    'Проверьте артикул — не совпадает с инвойсом',
                    'Требуется разрешение Роспотребнадзора для данной категории',
                ]
                comment = random.choice(comments)
            elif approval in ('approved', 'rejected'):
                approved_at = timezone.now() - timedelta(hours=random.randint(1, 48))

            goods_to_create.append(HAWBGood(
                hawb             = hawb,
                name             = name,
                tnved_code       = tnved,
                brand            = brand,
                manufacturer     = mfr,
                model            = model,
                article          = article,
                product_url      = product_url,
                quantity         = qty,
                unit             = unit,
                quantity_additional = quantity_additional,
                unit_additional  = unit_additional,
                weight_net       = (net_unit * qty).quantize(Decimal('0.001')),
                weight_gross     = (gross_unit * qty).quantize(Decimal('0.001')),
                unit_price       = unit_price,
                total_value      = total,
                currency         = cur,
                approval_status  = approval,
                approval_comment = comment,
                approved_at      = approved_at,
            ))

        HAWBGood.objects.bulk_create(goods_to_create)

    # ─────────────────────────────────────────────────────────────────────────
    def _advance(self, cargo, hawbs, target_stage, hawb_final_ls,
                 customs_scenario, days_ago, warehouse):
        hw_pks = [h.pk for h in hawbs]

        formed_at   = timezone.now() - timedelta(days=max(days_ago - 1, 1))
        dispatch_at = timezone.now() - timedelta(days=max(days_ago - 4, 1))
        arrived_at  = timezone.now() - timedelta(days=max(days_ago - 7, 1))
        svh_at      = arrived_at + timedelta(hours=4)
        customs_at  = svh_at + timedelta(hours=10)
        release_at  = customs_at + timedelta(days=3)

        departure_date_val = (cargo.flight_date - timedelta(days=2)) if cargo.flight_date else None

        if target_stage == 'DRAFT':
            return

        HouseWaybill.objects.filter(pk__in=hw_pks).update(
            logistics_status      = 'CONSOLIDATED',
            logistics_status_date = formed_at,
            last_status_change    = formed_at,
        )

        if target_stage == 'FORMED':
            if hawb_final_ls == 'READY_TO_SHIP':
                HouseWaybill.objects.filter(pk__in=hw_pks).update(
                    logistics_status      = 'READY_TO_SHIP',
                    logistics_status_date = formed_at + timedelta(hours=2),
                    last_status_change    = formed_at + timedelta(hours=2),
                )
            cargo.stage    = 'FORMED'
            cargo.is_draft = False
            cargo.stage_changed_at = formed_at
            cargo.save()
            return

        HouseWaybill.objects.filter(pk__in=hw_pks).update(
            logistics_status      = hawb_final_ls if target_stage == 'DISPATCHED'
                                     else 'IN_TRANSIT_EXP',
            logistics_status_date = dispatch_at,
            last_status_change    = dispatch_at,
        )

        if target_stage == 'DISPATCHED':
            cargo.stage          = 'DISPATCHED'
            cargo.is_draft       = False
            cargo.stage_changed_at = dispatch_at
            cargo.departure_date = departure_date_val
            cargo.save()
            return

        HouseWaybill.objects.filter(pk__in=hw_pks).update(
            logistics_status      = 'AT_SVH',
            logistics_status_date = svh_at,
            last_status_change    = svh_at,
            scan_into_bond        = svh_at,
        )

        if target_stage == 'ARRIVED':
            cargo.stage          = 'ARRIVED'
            cargo.is_draft       = False
            cargo.stage_changed_at = svh_at
            cargo.warehouse      = warehouse
            cargo.scan_into_bond = svh_at
            cargo.bond_location  = f'A-{random.randint(1, 30)}-{random.randint(1, 20)}'
            cargo.departure_date = departure_date_val
            cargo.save()
            return

        scenario = CUSTOMS_SCENARIOS.get(customs_scenario or 'mixed')
        for i, h in enumerate(hawbs):
            cs     = scenario[i % len(scenario)]
            has_td = cs in TD_REQUIRED_STATUSES
            td_num = _td(customs_at) if has_td else ''
            rel_dt = customs_at + timedelta(days=2) if cs == 'RELEASED' else None
            ls     = 'READY_DELIVERY' if cs == 'RELEASED' else 'IMPORT_CUSTOMS'
            HouseWaybill.objects.filter(pk=h.pk).update(
                logistics_status            = ls,
                logistics_status_date       = customs_at,
                customs_status              = cs,
                customs_status_date         = customs_at,
                last_status_change          = customs_at,
                scan_into_bond              = svh_at,
                customs_declaration_number  = td_num,
                release_date                = rel_dt,
            )

        if target_stage == 'CUSTOMS':
            cargo.stage          = 'CUSTOMS'
            cargo.is_draft       = False
            cargo.stage_changed_at = customs_at
            cargo.warehouse      = warehouse
            cargo.scan_into_bond = svh_at
            cargo.bond_location  = f'B-{random.randint(1, 30)}-{random.randint(1, 20)}'
            cargo.departure_date = departure_date_val
            cargo.save()
            return

        HouseWaybill.objects.filter(pk__in=hw_pks).update(
            logistics_status            = 'DELIVERED',
            logistics_status_date       = release_at,
            customs_status              = 'RELEASED',
            customs_status_date         = release_at,
            last_status_change          = release_at,
            scan_into_bond              = svh_at,
            customs_declaration_number  = _td(customs_at),
            release_date                = release_at,
        )
        cargo.stage              = 'RELEASED'
        cargo.is_draft           = False
        cargo.stage_changed_at   = release_at
        cargo.warehouse          = warehouse
        cargo.scan_into_bond     = svh_at
        cargo.scan_out_of_bond   = release_at + timedelta(hours=12)
        cargo.release_date       = release_at
        cargo.bond_location      = f'C-{random.randint(1, 30)}-{random.randint(1, 20)}'
        cargo.departure_date     = departure_date_val
        cargo.save()

    # ─────────────────────────────────────────────────────────────────────────
    def _create_standalone_hawbs(self, user) -> int:
        # Standalone HAWB (без партии) — только "предотправочные" статусы.
        # Статусы IN_TRANSIT_EXP/ARRIVED_DEST/AT_SVH/IMPORT_CUSTOMS/... физически
        # требуют привязки к партии и здесь недопустимы.
        pool = (
            ['AT_ORIGIN_WH']  * 9 +
            ['TO_ORIGIN_WH']  * 4 +
            ['CREATED']       * 3 +
            ['RETURNED']      * 1 +
            ['LOST']          * 1
        )
        random.shuffle(pool)

        created = 0
        for i, ls in enumerate(pool):
            consignee, inn, city = random.choice(CONSIGNEES)
            w   = Decimal(str(round(random.uniform(0.5, 40.0), 2)))
            pcs = random.randint(1, 4)
            desc_en, desc_ru = random.choice(DESCRIPTIONS)

            doc_inv   = random.random() > 0.3
            doc_pack  = random.random() > 0.4
            doc_perm  = random.random() > 0.6
            doc_tech  = random.random() > 0.7

            h = HouseWaybill(
                mawb             = None,
                hawb_number      = f'SA{random.randint(100, 999)}-{random.randint(10_000_000, 99_999_999)}',
                description      = desc_ru,
                cargo_type       = random.choice(CARGO_TYPES),
                shipment_type    = 'IMPORT',
                consignee_name   = consignee,
                consignee_inn    = inn,
                consignee_city   = city,
                weight           = w,
                pieces_declared  = pcs,
                invoice_value    = Decimal(str(round(random.uniform(50, 3000), 2))),
                invoice_currency = random.choice(CURRENCIES),
                logistics_status = ls,
                doc_invoice      = doc_inv,
                doc_packing_list = doc_pack,
                doc_permit       = doc_perm,
                doc_tech_desc    = doc_tech,
                docs_required    = 4,
                last_status_change = timezone.now() - timedelta(hours=random.randint(1, 72)),
            )
            h.save()
            # Standalone: этап близок к DRAFT
            self._create_goods(h, 'DRAFT')
            created += 1

        return created
