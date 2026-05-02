"""
Команда для добавления тестовых HAWB к существующим партиям
Запуск: python manage.py load_hawb_data
"""
import os
import random
import secrets
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from cargo.models import Cargo, HouseWaybill

STATUSES = ['CNPK', 'CIDM', 'CVAL', 'DUTY', 'HOLD', 'EXAM', 'HLDP', 'RLSE', 'REJ', 'NCI', 'MPWI']

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

DESCRIPTIONS = [
    'Электронные компоненты', 'Запчасти для автомобилей',
    'Медицинское оборудование', 'Одежда и аксессуары',
    'Косметика', 'Пищевые добавки', 'Оптические инструменты',
    'Текстильные товары', 'Запасные части', 'Промышленное оборудование',
]


class Command(BaseCommand):
    help = 'Добавляет тестовые HAWB ко всем существующим партиям MAWB'

    def add_arguments(self, parser):
        parser.add_argument('--per-mawb', type=int, default=5,
                            help='Количество HAWB на каждую партию (по умолчанию 5)')
        parser.add_argument('--clear', action='store_true',
                            help='Удалить все существующие HAWB перед созданием')

    def handle(self, *args, **options):
        per_mawb = options['per_mawb']

        if options['clear']:
            count = HouseWaybill.objects.count()
            HouseWaybill.objects.all().delete()
            self.stdout.write(f'  Удалено {count} существующих HAWB')

        # Получаем пользователей для назначения
        users = list(User.objects.filter(is_active=True))
        if not users:
            pw = os.environ.get('CARGO_TESTUSER_PASSWORD') or secrets.token_urlsafe(16)
            users = [User.objects.create_user('testuser', password=pw)]
            self.stdout.write(self.style.WARNING(
                f'Создан testuser с паролем: {pw} '
                '(переопределите через CARGO_TESTUSER_PASSWORD)'
            ))

        cargos = list(Cargo.objects.all())
        if not cargos:
            self.stdout.write(self.style.ERROR('Нет партий MAWB! Сначала запусти load_test_data'))
            return

        self.stdout.write(f'Создаю HAWB для {len(cargos)} партий (по {per_mawb} штук)...')

        created = 0
        skipped = 0
        existing = set(HouseWaybill.objects.values_list('hawb_number', flat=True))

        for cargo in cargos:
            # Гарантируем что все статусы встречаются хотя бы по одному разу
            # для первых 11 партий — по одному уникальному статусу
            if cargos.index(cargo) < len(STATUSES):
                forced_statuses = [STATUSES[cargos.index(cargo)]]
                extra = per_mawb - 1
            else:
                forced_statuses = []
                extra = per_mawb

            all_statuses = forced_statuses + random.choices(STATUSES, k=extra)
            random.shuffle(all_statuses)

            svh_base = cargo.scan_into_bond or (timezone.now() - timedelta(days=random.randint(1, 30)))

            for j, status in enumerate(all_statuses):
                hawb_num = f'H{cargo.awb_number.replace("-","")[-7:]}{j+1:02d}'
                if hawb_num in existing:
                    skipped += 1
                    continue

                consignee, inn = random.choice(CONSIGNEES)
                pieces_decl = random.randint(1, 5)

                svh_time = svh_base + timedelta(hours=random.randint(1, 12))

                # Документы — зависят от статуса
                if status == 'RLSE':
                    doc_inv, doc_pack, doc_perm, doc_tech = True, True, True, True
                elif status in ('DUTY', 'EXAM'):
                    doc_inv, doc_pack = True, True
                    doc_perm = random.random() > 0.5
                    doc_tech = random.random() > 0.5
                elif status in ('CIDM', 'CVAL'):
                    doc_inv = random.random() > 0.3
                    doc_pack = random.random() > 0.5
                    doc_perm, doc_tech = False, False
                else:
                    doc_inv = random.random() > 0.6
                    doc_pack = random.random() > 0.7
                    doc_perm = random.random() > 0.8
                    doc_tech = random.random() > 0.8

                release = None
                if status == 'RLSE':
                    release = svh_time + timedelta(days=random.randint(2, 10))

                td_number = ''
                if status in ('DUTY', 'RLSE', 'EXAM', 'HLDP'):
                    td_number = f'10{random.randint(100,999)}/{svh_time.strftime("%d%m%y")}/{random.randint(1000000,9999999)}'

                HouseWaybill.objects.create(
                    mawb=cargo,
                    hawb_number=hawb_num,
                    description=random.choice(DESCRIPTIONS),
                    cargo_type=random.choice(['B2C', 'B2B', 'C2C']),
                    shipment_type=random.choice(['IMPORT', 'EXPORT']),
                    consignee_name=consignee,
                    consignee_inn=inn,
                    consignee_city=random.choice(['Москва', 'Санкт-Петербург', 'Казань', 'Екатеринбург', 'Новосибирск']),
                    weight=round(random.uniform(0.5, 80), 2),
                    pieces_declared=pieces_decl,
                    invoice_value=round(random.uniform(50, 3000), 2),
                    invoice_currency=random.choice(['USD', 'EUR', 'CNY']),
                    status=status,
                    last_status_change=timezone.now() - timedelta(hours=random.randint(1, 200)),
                    assigned_to=random.choice(users) if random.random() > 0.3 else None,
                    scan_into_bond=svh_time,
                    customs_declaration_number=td_number,
                    release_date=release,
                    doc_invoice=doc_inv,
                    doc_packing_list=doc_pack,
                    doc_permit=doc_perm,
                    doc_tech_desc=doc_tech,
                    notes='Тестовые данные',
                )
                existing.add(hawb_num)
                created += 1

        self.stdout.write(self.style.SUCCESS(
            f'Готово! Создано: {created} HAWB, пропущено (дубли): {skipped}'
        ))
        self.stdout.write(f'Всего HAWB в базе: {HouseWaybill.objects.count()}')
