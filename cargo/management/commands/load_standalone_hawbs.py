"""
Создаёт standalone HAWB (без партий) с товарами и документами.
Соблюдает бизнес-логику:
  1. Без партии → нет даты СВХ
  2. В пути → нет даты СВХ
  3. Нет СВХ → нет лицензии склада
  4. Неполный чеклист → нет рег. номера ДТ
"""
import random
from datetime import date, timedelta
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from cargo.models import HouseWaybill, HAWBGood, HAWBDocument

# Standalone HAWB (без привязки к партии) — только "предотправочные" статусы.
# Статусы "в пути/прибыл/на СВХ/оформление/доставлен" физически требуют партии
# и здесь недопустимы (см. HouseWaybill.REQUIRES_MAWB_STATUSES).
STATUSES_STANDALONE = ["CREATED", "TO_ORIGIN_WH", "AT_ORIGIN_WH",
                       "CONSOLIDATED", "READY_TO_SHIP", "EXPORT_CUSTOMS",
                       "RETURNED", "LOST"]

CONSIGNEES = [
    ('ООО Техно-Трейд',    '7701234567',   'Москва',           '+7 495 123-45-67'),
    ('ИП Иванов А.А.',      '503456789012', 'Санкт-Петербург',  '+7 812 234-56-78'),
    ('АО МедПоставка',      '7723456789',   'Москва',           '+7 495 345-67-89'),
    ('ЗАО ИмпортМаш',       '7745678901',   'Екатеринбург',     '+7 343 456-78-90'),
    ('ООО Элит Груп',       '7756789012',   'Казань',           '+7 843 567-89-01'),
    ('ИП Петрова М.С.',     '504567890123', 'Новосибирск',      '+7 383 678-90-12'),
    ('АО ФармИмпорт',       '7778901234',   'Москва',           '+7 495 789-01-23'),
    ('ООО ТрейдСервис',     '7789012345',   'Краснодар',        '+7 861 890-12-34'),
    ('ООО СпортЛайф',       '7790123456',   'Москва',           '+7 495 901-23-45'),
    ('ЗАО ЭлектроСистемы',  '7712345678',   'Самара',           '+7 846 012-34-56'),
]

GOODS_CATALOG = [
    ('Смартфон',                   '8517120000', 'Apple',      'Apple Inc.',           'iPhone 15 Pro',      'A3292',      'шт',  999.00),
    ('Ноутбук',                    '8471300000', 'Lenovo',     'Lenovo Group',         'ThinkPad X1 Carbon', '20XW0023',   'шт',  1499.00),
    ('Планшет',                    '8471300000', 'Samsung',    'Samsung Electronics',  'Galaxy Tab S9',      'SM-X710',    'шт',  649.00),
    ('Наушники беспроводные',      '8518300000', 'Sony',       'Sony Corporation',     'WH-1000XM5',         'WH1000XM5',  'шт',  299.00),
    ('Электродвигатель',           '8501520000', 'Siemens',    'Siemens AG',           '1LE1002',            '1LE10021CA', 'шт',  450.00),
    ('Подшипник шариковый',        '8482100090', 'SKF',        'SKF Group',            '6205-2RS',           '6205-2RSH',  'шт',  12.50),
    ('Контроллер программируемый', '8537101900', 'Schneider',  'Schneider Electric',   'M241',               'TM241C24T',  'шт',  380.00),
    ('Крем для лица',              '3304990000', 'La Mer',     'Estée Lauder',         'Moisturizing Cream', '1030100',    'шт',  185.00),
    ('Витаминный комплекс',        '2106909800', 'Solgar',     'Solgar Inc.',          'Formula VM-75',      '36224',      'уп',  45.00),
    ('Медицинский зонд',           '9018909090', 'Olympus',    'Olympus Corporation',  'GIF-H290',           'GIF-H290Z',  'шт',  8500.00),
    ('Масляный фильтр',            '8421230000', 'Mann',       'Mann+Hummel',          'W 940/25',           'W940/25',    'шт',  18.00),
    ('Ткань хлопковая',            '5208210000', 'Dormeuil',   'Dormeuil Frères',      'Amadeus',            'A25014',     'м',   120.00),
    ('Промышленный насос',         '8413709100', 'Grundfos',   'Grundfos A/S',         'CM5-5',              'CM5-5A',     'шт',  620.00),
    ('Кофемашина',                 '8516710000', "De'Longhi",  "De'Longhi Group",      'Magnifica Evo',      'ECAM29051',  'шт',  699.00),
    ('Принтер этикеток',           '8443321000', 'Zebra',      'Zebra Technologies',   'ZT411',              'ZT41142',    'шт',  2100.00),
    ('Сенсор давления',            '9026201900', 'Endress',    'Endress+Hauser',        'Cerabar M',          'PMC51',      'шт',  890.00),
    ('Спортивный велосипед',       '8712009100', 'Trek',       'Trek Bicycle Corp.',   'Marlin 6',           'MARL6GN',    'шт',  749.00),
    ('Конструктор LEGO',           '9503000090', 'LEGO',       'LEGO Group',           'Technic 42143',      '42143',      'шт',  189.00),
    ('Промышленный кабель',        '8544421900', 'Nexans',     'Nexans S.A.',          'NYY-J 4x16',         'NYYJ416',    'м',   8.50),
    ('Автомобильный аккумулятор',  '8507100090', 'Bosch',      'Robert Bosch GmbH',    'S4 Silver',          'S4E05',      'шт',  110.00),
]


class Command(BaseCommand):
    help = 'Создаёт standalone HAWB без партий с товарами и документами'

    def add_arguments(self, parser):
        parser.add_argument('--count', type=int, default=20)
        parser.add_argument('--clear', action='store_true',
                            help='Удалить существующие standalone HAWB перед созданием')

    def handle(self, *args, **options):
        if options['clear']:
            cnt = HouseWaybill.objects.filter(mawb__isnull=True).count()
            HouseWaybill.objects.filter(mawb__isnull=True).delete()
            self.stdout.write(f'  Удалено {cnt} standalone HAWB')

        users = list(User.objects.filter(is_active=True))
        if not users:
            self.stdout.write(self.style.ERROR('Нет пользователей!'))
            return

        count = options['count']
        existing = set(HouseWaybill.objects.values_list('hawb_number', flat=True))

        # Равномерно распределяем статусы (только допустимые для standalone)
        status_pool = (STATUSES_STANDALONE * (count // len(STATUSES_STANDALONE) + 1))[:count]
        random.shuffle(status_pool)

        created_hawbs = created_goods = created_docs = 0

        for i in range(count):
            # Уникальный номер
            for _ in range(30):
                hawb_num = f'SA{random.randint(100,999)}-{random.randint(10000000,99999999)}'
                if hawb_num not in existing:
                    break

            consignee, inn, city, phone = random.choice(CONSIGNEES)
            status = status_pool[i]
            cargo_type = random.choice(['B2C', 'B2B', 'B2B', 'C2C'])
            shipment_type = random.choice(['IMPORT', 'IMPORT', 'IMPORT', 'EXPORT'])

            # Товары (1–4 позиции)
            n_goods = random.randint(1, 4)
            goods_sel = random.sample(GOODS_CATALOG, n_goods)
            total_weight = round(sum(random.uniform(0.3, 12) for _ in goods_sel), 2)
            currency = random.choice(['USD', 'USD', 'EUR', 'CNY'])
            total_value = round(sum(g[7] * random.randint(1, 5) for g in goods_sel), 2)

            # ── Документы ──
            # Для NCI инвойса нет по определению
            if status == 'NCI':
                doc_inv, doc_pack = False, random.random() > 0.5
                doc_perm, doc_tech = False, False
            elif status in ('CVAL', 'DUTY'):
                doc_inv, doc_pack = True, True
                doc_perm = random.random() > 0.4
                doc_tech = random.random() > 0.4
            elif status == 'CIDM':
                doc_inv = random.random() > 0.3
                doc_pack = random.random() > 0.5
                doc_perm, doc_tech = False, False
            elif status == 'REJ':
                # Отклонён — документы частичные
                doc_inv = random.random() > 0.5
                doc_pack = random.random() > 0.5
                doc_perm = random.random() > 0.7
                doc_tech = random.random() > 0.7
            else:
                doc_inv = random.random() > 0.6
                doc_pack = random.random() > 0.7
                doc_perm = random.random() > 0.85
                doc_tech = random.random() > 0.85

            docs_count = sum([doc_inv, doc_pack, doc_perm, doc_tech])
            docs_required = random.choice([3, 4, 4])
            docs_complete = docs_count >= docs_required

            # ── Правило 1 & 2: standalone HAWB → НЕТ даты СВХ ──
            # Груз без партии всегда считается "в пути" / ещё не прилетел
            svh_time = None  # всегда None для standalone

            # ── Правило 4: неполный чеклист → НЕТ рег. номера ДТ ──
            td_number = ''
            if docs_complete:
                # Только если все документы собраны
                td_number = f'10{random.randint(100,999)}/{timezone.now().strftime("%d%m%y")}/{random.randint(1000000,9999999)}'

            release_date = None
            # standalone HAWB не может быть RLSE — нет СВХ, нет оформления

            hawb = HouseWaybill(
                mawb=None,  # ← независимая накладная
                hawb_number=hawb_num,
                description=', '.join(g[0] for g in goods_sel),
                cargo_type=cargo_type,
                shipment_type=shipment_type,
                consignee_name=consignee,
                consignee_inn=inn,
                consignee_city=city,
                consignee_phone=phone,
                consignee_address=f'ул. Тестовая, д.{random.randint(1,200)}, оф.{random.randint(1,100)}',
                weight=total_weight,
                pieces_declared=n_goods,
                invoice_value=total_value,
                invoice_currency=currency,
                logistics_status=status,
                last_status_change=timezone.now() - timedelta(hours=random.randint(1, 72)),
                assigned_to=random.choice(users) if random.random() > 0.3 else None,
                scan_into_bond=svh_time,       # None — правило 1
                customs_declaration_number=td_number,  # пусто если неполный чеклист — правило 4
                release_date=release_date,
                doc_invoice=doc_inv,
                doc_packing_list=doc_pack,
                doc_permit=doc_perm,
                doc_tech_desc=doc_tech,
                docs_required=docs_required,
                notes='Standalone HAWB — груз готовится к отправке или в пути',
            )
            hawb.save()  # save() применит бизнес-правила автоматически
            existing.add(hawb_num)
            created_hawbs += 1

            # ── Товарные позиции ──
            # ДЕИ-словарь по артикулу (упаковочные нормы)
            DEI_MAP = {
                'TEE-12X': (12, 'шт'), 'SOX-6PR': (6, 'пар'),
                'MASK-50': (50, 'шт'), 'SYR-5ML': (100, 'шт'),
                'GLOVE-L': (100, 'пар'), 'SPARK-NGK': (4, 'шт'),
                'JACKET-L': (1, 'шт'), 'JEAN-3232': (1, 'шт'),
            }
            for gdata in goods_sel:
                name, tnved, brand, mfr, model, article, unit, base_price = gdata
                qty = random.randint(1, 8)
                price = round(base_price * random.uniform(0.9, 1.1), 2)
                w_per = round(total_weight / n_goods, 3)

                # ДЕИ
                dei = DEI_MAP.get(article)
                quantity_additional = qty * dei[0] if dei else None
                unit_additional = dei[1] if dei else ''

                # B2C — обязательна ссылка на товар
                if cargo_type == 'B2C':
                    slug = (article or 'p').lower().replace('/', '-').replace(' ', '-')
                    product_url = f'https://shop.example.com/p/{slug}-{random.randint(1000, 9999)}'
                else:
                    product_url = ''

                HAWBGood.objects.create(
                    hawb=hawb,
                    name=name,
                    tnved_code=tnved,
                    brand=brand,
                    manufacturer=mfr,
                    model=model,
                    article=article,
                    product_url=product_url,
                    quantity=qty,
                    unit=unit,
                    quantity_additional=quantity_additional,
                    unit_additional=unit_additional,
                    weight_net=round(w_per * 0.9, 3),
                    weight_gross=w_per,
                    unit_price=price,
                    total_value=round(price * qty, 2),
                    currency=currency,
                )
                created_goods += 1

            # ── Документы ──
            doc_map = {
                'invoice':      ('Инвойс',                  f'INV-{random.randint(1000,9999)}',  doc_inv),
                'packing_list': ('Упаковочный лист',         f'PL-{random.randint(1000,9999)}',   doc_pack),
                'permit':       ('Разрешение на ввоз',       f'PERM-{random.randint(100,999)}',   doc_perm),
                'tech_desc':    ('Техническое описание',     f'TD-{random.randint(1000,9999)}',   doc_tech),
            }
            for dtype, (dname, dnum, received) in doc_map.items():
                if received or random.random() > 0.5:
                    HAWBDocument.objects.create(
                        hawb=hawb,
                        doc_type=dtype,
                        name=dname,
                        number=dnum,
                        issue_date=date.today() - timedelta(days=random.randint(3, 45)),
                        is_received=received,
                    )
                    created_docs += 1

        self.stdout.write(self.style.SUCCESS(
            f'\nГотово!\n'
            f'  HAWB создано:          {created_hawbs}\n'
            f'  Товарных позиций:      {created_goods}\n'
            f'  Документов:            {created_docs}\n'
            f'  Всего standalone HAWB: {HouseWaybill.objects.filter(mawb__isnull=True).count()}\n'
            f'\nБизнес-правила применены:\n'
            f'  ✓ Дата СВХ = None (нет партии)\n'
            f'  ✓ Рег. номер ДТ только при полном чеклисте\n'
            f'  ✓ NCI-статус → инвойс отсутствует\n'
            f'  ✓ Мест прибыло = 0 (груз ещё в пути)'
        ))
