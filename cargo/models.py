"""
Модели данных для системы отслеживания грузов CargoTrack Pro
"""
import logging
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

logger = logging.getLogger('cargo')


# ─────────────────────────── CHOICES ───────────────────────────

SHP_TYPE_CHOICES = [
    ('IMPEX', 'IMPEX — Импорт/Экспорт'),
    ('B2C', 'B2C — Бизнес для потребителя'),
    ('B2B', 'B2B — Бизнес для бизнеса'),
    ('DIP', 'DIP — Дипломатический'),
]

STAGE_CHOICES = [
    ('DRAFT',      'Формирование партии'),
    ('FORMED',     'Партия сформирована'),
    ('DISPATCHED', 'Партия отправлена в страну назначения'),
    ('ARRIVED',    'Партия прибыла в страну назначения'),
    ('CUSTOMS',    'Таможенное оформление в стране назначения'),
    ('RELEASED',   'Полный выпуск'),
]

RTO_REASON_CHOICES = [
    ('REFUSAL_DOX',    'Отказ — документы'),
    ('NO_DOX',         'Нет документов'),
    ('PRIVATE',        'Частный получатель'),
    ('UNSERVICED',     'Необслуживаемый регион'),
    ('UNDERVALUE',     'Занижение стоимости'),
    ('PROHIBITED',     'Запрещённый товар'),
    ('NCI',            'Нет коммерческого инвойса'),
    ('RTO_REQUEST',    'Запрос возврата'),
    ('DDP',            'DDP — доставка с оплатой пошлин'),
    ('PAYMENT_REFUSAL','Отказ от оплаты'),
    ('MISCODE',        'Неверный код товара'),
]

CPC_CODE_CHOICES = [
    ('BILL_WT',  'BILL WT — Оплата по весу'),
    ('CED',      'CED — Таможенная декларация'),
    ('DOCS',     'DOCS — Документы'),
    ('DRT',      'DRT — Прямой маршрут'),
    ('FITO',     'FITO — Фитосанитарный'),
    ('FRG_SHP',  'FRG SHP — Фрахт'),
    ('PRIVAT',   'PRIVAT — Личный груз'),
    ('RAPID',    'RAPID — Срочная доставка'),
    ('STP_CUST', 'STP CUST — Остановлен таможней'),
    ('SVOLOW0',  'SVOLOW0'),
    ('SVOLOW1',  'SVOLOW1'),
    ('SVOLOW2',  'SVOLOW2'),
    ('SVOLOW3',  'SVOLOW3'),
    ('SVOLOW4',  'SVOLOW4'),
    ('SVOLOW5',  'SVOLOW5'),
]

TRANSPORT_MODE_CHOICES = [
    (1, 'Морской'),
    (2, 'Железнодорожный'),
    (3, 'Автомобильный'),
    (4, 'Авиа'),
    (5, 'Почтовый'),
]

CURRENCY_CHOICES = [
    ('USD', 'USD — Доллар США'),
    ('EUR', 'EUR — Евро'),
    ('CNY', 'CNY — Китайский юань'),
    ('GBP', 'GBP — Фунт стерлингов'),
    ('RUB', 'RUB — Российский рубль'),
]


# ─────────────────────────── МОДЕЛИ ───────────────────────────

class Warehouse(models.Model):
    """Справочник складов СВХ"""
    name = models.CharField('Название склада', max_length=200)
    license_number = models.CharField('№ лицензии СВХ', max_length=50, unique=True)
    address = models.TextField('Адрес', blank=True)
    city = models.CharField('Город', max_length=100, blank=True)
    iata_code = models.CharField('IATA код', max_length=3, blank=True)
    contact_person = models.CharField('Контактное лицо', max_length=200, blank=True)
    phone = models.CharField('Телефон', max_length=50, blank=True)
    email = models.EmailField('Email', blank=True)
    max_capacity_kg = models.DecimalField('Макс. ёмкость (кг)', max_digits=12, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField('Активен', default=True)

    class Meta:
        verbose_name = 'Склад СВХ'
        verbose_name_plural = 'Склады СВХ'
        ordering = ['name']

    def __str__(self) -> str:
        return f'{self.name} ({self.license_number})'


class Flight(models.Model):
    """Справочник рейсов"""
    flight_number = models.CharField('Номер рейса', max_length=20)
    flight_date = models.DateField('Дата рейса')
    airline = models.CharField('Авиакомпания', max_length=100, blank=True)
    departure_iata = models.CharField('IATA отправления', max_length=3)
    arrival_iata = models.CharField('IATA прибытия', max_length=3)
    scheduled_departure = models.DateTimeField('Плановый вылет', null=True, blank=True)
    scheduled_arrival = models.DateTimeField('Плановое прибытие', null=True, blank=True)
    actual_departure = models.DateTimeField('Фактический вылет', null=True, blank=True)
    actual_arrival = models.DateTimeField('Фактическое прибытие', null=True, blank=True)
    status = models.CharField('Статус рейса', max_length=50, blank=True)

    class Meta:
        verbose_name = 'Рейс'
        verbose_name_plural = 'Рейсы'
        ordering = ['-flight_date', 'flight_number']
        unique_together = [('flight_number', 'flight_date')]

    def __str__(self) -> str:
        return f'{self.flight_number} {self.flight_date} ({self.departure_iata}→{self.arrival_iata})'


class Label(models.Model):
    """Метка (тег) для пометки партий и накладных. Используется в фильтрах CQL."""
    name = models.CharField('Название', max_length=64, unique=True, db_index=True)
    color = models.CharField('Цвет (hex)', max_length=7, default='#6c757d',
                             help_text='HEX-код цвета, например #ff5722')
    description = models.TextField('Описание', blank=True, default='')
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL,
                                   null=True, blank=True,
                                   verbose_name='Создал',
                                   related_name='created_labels')

    class Meta:
        verbose_name = 'Метка'
        verbose_name_plural = 'Метки'
        ordering = ['name']

    def __str__(self) -> str:
        return self.name


class Cargo(models.Model):
    """Основная модель груза"""

    # ── Идентификация ──
    awb_number = models.CharField('AWB номер', max_length=30, unique=True, db_index=True)
    description = models.TextField('Описание груза (EN)', blank=True)
    description_ru = models.TextField('Описание груза (RU)', blank=True)

    # ── Типы ──
    shp_type = models.CharField('Тип отправителя', max_length=10, choices=SHP_TYPE_CHOICES, blank=True)
    cpc_code = models.CharField('CPC код', max_length=20, choices=CPC_CODE_CHOICES, blank=True)

    # ── Этап ──
    stage = models.CharField('Этап', max_length=15, choices=STAGE_CHOICES,
                             default='DRAFT', db_index=True)
    is_draft = models.BooleanField('Черновик', default=True,
                                   help_text='Черновик — партия ещё формируется')
    last_status_change = models.DateTimeField('Последнее изменение этапа', null=True, blank=True)
    stage_changed_at = models.DateTimeField('Дата смены этапа', null=True, blank=True)

    # ── Рейс ──
    flight_number = models.CharField('Номер рейса', max_length=20, blank=True)
    departure_date = models.DateField('Дата вылета', null=True, blank=True,
                                      help_text='Недоступно на этапах Формирование / Сформирована')
    flight_date = models.DateField('Дата прилёта', null=True, blank=True)
    departure_iata = models.CharField('IATA отправления', max_length=3, blank=True)
    arrival_iata = models.CharField('IATA прибытия', max_length=3, blank=True)
    movement_number = models.CharField('Номер муверского', max_length=50, blank=True)
    transportation_mode = models.IntegerField('Вид транспорта', choices=TRANSPORT_MODE_CHOICES, default=4)

    # ── Параметры груза ──
    weight = models.DecimalField('Вес (кг)', max_digits=10, decimal_places=2, null=True, blank=True)
    pieces_declared = models.IntegerField('Мест', default=0)

    # ── Стоимость ──
    invoice_currency = models.CharField('Валюта инвойса', max_length=3, choices=CURRENCY_CHOICES, default='USD')
    invoice_value = models.DecimalField('Стоимость по инвойсу', max_digits=14, decimal_places=2, null=True, blank=True)
    customs_value_rub = models.DecimalField('Таможенная стоимость (RUB)', max_digits=14, decimal_places=2, null=True, blank=True)
    duty_amount = models.DecimalField('Сумма пошлины', max_digits=14, decimal_places=2, null=True, blank=True)

    # ── Склад СВХ ──
    warehouse = models.ForeignKey(Warehouse, on_delete=models.SET_NULL, null=True, blank=True,
                                  verbose_name='Склад СВХ', related_name='cargos')
    warehouse_name = models.CharField('Название склада', max_length=200, blank=True)
    warehouse_license = models.CharField('№ лицензии склада', max_length=50, blank=True)
    bond_location = models.CharField('Ячейка хранения', max_length=100, blank=True)
    scan_into_bond = models.DateTimeField('Въезд на склад', null=True, blank=True)
    scan_out_of_bond = models.DateTimeField('Выезд со склада', null=True, blank=True)

    # ── Таможня ──
    customs_declaration_number = models.CharField('Номер ТД', max_length=50, blank=True, db_index=True)
    entry_date = models.DateTimeField('Дата подачи декларации', null=True, blank=True)
    release_date = models.DateTimeField('Дата выпуска', null=True, blank=True)

    # ── Транзит ──
    is_transit = models.BooleanField('Транзитный груз', default=False)
    bonded_dest = models.CharField('Таможенный пункт назначения', max_length=100, blank=True)
    bonded_transit = models.CharField('Таможенный транзит', max_length=100, blank=True)

    # ── Сценарий ТО ──
    # Если True — клиент таможит груз сам, мы только привозим его на СВХ.
    is_self_clearance = models.BooleanField(
        'ТО клиентом', default=False,
        help_text='Клиент сам выпускает груз; наш брокер не задействован',
    )

    # ── RTO ──
    rto_reason = models.CharField('Причина RTO', max_length=30, choices=RTO_REASON_CHOICES, blank=True)

    # ── UDF поля ──
    payer_account = models.CharField('Счёт плательщика', max_length=50, blank=True)
    shipper_account = models.CharField('Счёт отправителя', max_length=50, blank=True)

    # ── Системные поля ──
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   verbose_name='Создал', related_name='created_cargos')
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлён', auto_now=True)

    # ── Метки (теги для фильтрации) ──
    labels = models.ManyToManyField(Label, blank=True, related_name='cargos',
                                    verbose_name='Метки')

    class Meta:
        verbose_name = 'Груз'
        verbose_name_plural = 'Грузы'
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'AWB {self.awb_number} [{self.get_status_display()}]'

    @property
    def stage_display(self) -> str:
        return dict(STAGE_CHOICES).get(self.stage, self.stage)

    @property
    def stage_color(self) -> str:
        return {
            'DRAFT':      'secondary',
            'FORMED':     'info',
            'DISPATCHED': 'primary',
            'ARRIVED':    'warning',
            'CUSTOMS':    'warning',
            'RELEASED':   'success',
        }.get(self.stage, 'secondary')

    @property
    def can_advance_stage(self) -> bool:
        """Можно ли перейти на следующий этап"""
        stages = [s[0] for s in STAGE_CHOICES]
        idx = stages.index(self.stage) if self.stage in stages else -1
        return idx < len(stages) - 1

    @property
    def all_hawbs_released(self) -> bool:
        """Все HAWB в партии выпущены (таможенный статус RELEASED)"""
        hawbs = self.hawbs.all()
        if not hawbs.exists():
            return False
        return all(h.customs_status == 'RELEASED' for h in hawbs)

    @property
    def released_weight(self) -> float:
        """Суммарный вес по выпущенным HAWB"""
        return float(self.hawbs.filter(
            customs_status='RELEASED'
        ).aggregate(s=models.Sum('weight'))['s'] or 0)

    @property
    def full_release_check(self) -> bool:
        """Полный выпуск: вес выпущенных HAWB совпадает с весом партии на СВХ"""
        if not self.weight or not self.scan_into_bond:
            return False
        return abs(self.released_weight - float(self.weight)) < 0.01

    def advance_stage(self, user=None) -> str:
        """Перевести партию на следующий этап"""
        stages = [s[0] for s in STAGE_CHOICES]
        idx = stages.index(self.stage) if self.stage in stages else -1
        if idx < len(stages) - 1:
            next_stage = stages[idx + 1]
            self.set_stage(next_stage, user)
            return next_stage
        return self.stage

    def set_stage(self, new_stage: str, user=None) -> None:
        """Установить этап"""
        self.stage = new_stage
        self.stage_changed_at = timezone.now()

        # Закрываем черновик при первом продвижении
        if new_stage != 'DRAFT':
            self.is_draft = False

        # Автоматические действия
        if new_stage == 'ARRIVED' and not self.scan_into_bond:
            self.scan_into_bond = timezone.now()
        if new_stage == 'RELEASED':
            self.release_date = self.release_date or timezone.now()

        self.save()
        logger.info(f'Партия {self.awb_number}: этап → {new_stage} ({user})')
        # Продвинуть активные воркфлоу
        from . import workflow_runner
        workflow_runner.advance_on_status_change(self, 'cargo', 'stage', new_stage)

    def close_draft(self, user=None) -> None:
        """Закрыть черновик — партия сформирована.

        Правило: суммарный вес накладных должен совпадать с весом партии.
        """
        if self.weight and self.hawbs.exists():
            total = self.hawbs_total_weight
            if abs(total - float(self.weight)) > 0.01:
                raise ValueError(
                    f'Невозможно сформировать партию: суммарный вес накладных '
                    f'({total:.2f} кг) не совпадает с весом партии '
                    f'({float(self.weight):.2f} кг). '
                    f'Скорректируйте веса накладных или общий вес партии.'
                )
        self.is_draft = False
        self.set_stage('FORMED', user)

    def check_auto_stage(self) -> None:
        """Автоматически проверяет нужно ли перейти на RELEASED"""
        if self.stage == 'CUSTOMS' and self.full_release_check:
            self.set_stage('RELEASED')

    # ── Весовая логика ──

    @property
    def hawbs_total_weight(self) -> float:
        """Суммарный вес всех HAWB в партии"""
        return float(self.hawbs.aggregate(s=models.Sum('weight'))['s'] or 0)

    # Этапы, на которых допускается корректировка веса
    WEIGHT_CORRECTION_STAGES = {'DISPATCHED', 'ARRIVED'}

    @property
    def can_correct_weight(self) -> bool:
        """Можно ли корректировать вес партии (только на этапах DISPATCHED / ARRIVED)"""
        return self.stage in self.WEIGHT_CORRECTION_STAGES

    def redistribute_hawb_weights(self, new_total_weight: float, user=None) -> None:
        """Пропорционально пересчитать веса HAWB при изменении общего веса партии.

        Бизнес-контекст: вес по AWB может отличаться на разных этапах
        (склад отправки, аэропорт, страна назначения), к тому же вес
        посылок не учитывает вес грузовых мест/тары.
        """
        if self.stage not in self.WEIGHT_CORRECTION_STAGES:
            raise ValueError(
                f'Корректировка веса доступна только на этапах '
                f'{", ".join(self.WEIGHT_CORRECTION_STAGES)}. Текущий: {self.stage}'
            )

        hawbs = list(self.hawbs.all())
        if not hawbs:
            self.weight = new_total_weight
            self.save()
            return

        old_total = sum(float(h.weight or 0) for h in hawbs)

        if old_total > 0:
            # Пропорциональное перераспределение
            ratio = new_total_weight / old_total
            for h in hawbs:
                if h.weight:
                    h.weight = round(float(h.weight) * ratio, 2)
                    h.save(update_fields=['weight', 'updated_at'])
        else:
            # Если у накладных нет весов — распределяем равномерно
            equal_weight = round(new_total_weight / len(hawbs), 2)
            for h in hawbs:
                h.weight = equal_weight
                h.save(update_fields=['weight', 'updated_at'])

        self.weight = new_total_weight
        self.save()
        logger.info(
            f'Партия {self.awb_number}: корректировка веса → {new_total_weight} кг, '
            f'пропорциональный пересчёт {len(hawbs)} HAWB ({user})'
        )



    @property
    def days_in_warehouse(self) -> int | None:
        """Количество дней на складе"""
        if not self.scan_into_bond:
            return None
        end = self.scan_out_of_bond or timezone.now()
        delta = end - self.scan_into_bond
        return delta.days

    @property
    def is_problematic(self) -> bool:
        """Проблемный груз: долго на складе (>7 дней)."""
        days = self.days_in_warehouse
        if days is not None and days > 7:
            return True
        return False

    @property
    def storage_end_date(self):
        """Дата окончания хранения на СВХ (120 дней по законодательству)"""
        if not self.scan_into_bond:
            return None
        from datetime import timedelta
        return self.scan_into_bond + timedelta(days=120)

    @property
    def storage_paid_start_date(self):
        """Дата начала платного хранения — дата ДО1 (scan_into_bond) + 4 дня"""
        if not self.scan_into_bond:
            return None
        from datetime import timedelta
        return self.scan_into_bond + timedelta(days=4)

    @property
    def storage_days_left(self):
        """Дней до окончания хранения"""
        end = self.storage_end_date
        if not end:
            return None
        delta = end - timezone.now()
        return max(0, delta.days)

    @property
    def is_paid_storage(self):
        """Началось ли платное хранение (с 4 календарного дня)"""
        paid = self.storage_paid_start_date
        if not paid:
            return False
        return timezone.now() >= paid

    @property
    def hours_in_status(self) -> float | None:
        """Количество часов с последней смены статуса"""
        if not self.last_status_change:
            return None
        delta = timezone.now() - self.last_status_change
        return round(delta.total_seconds() / 3600, 1)

    @property
    def status_timer_display(self) -> str:
        """Отображение таймера: '2ч 30м' или '3д 5ч'"""
        hours = self.hours_in_status
        if hours is None:
            return '—'
        if hours < 1:
            minutes = int(hours * 60)
            return f'{minutes}м'
        if hours < 24:
            h = int(hours)
            m = int((hours - h) * 60)
            return f'{h}ч {m}м' if m else f'{h}ч'
        days = int(hours // 24)
        rem_h = int(hours % 24)
        return f'{days}д {rem_h}ч' if rem_h else f'{days}д'


        """Смена статуса с созданием записи в истории"""
        old_status = self.status
        self.status = new_status
        self.last_status_change = timezone.now()

        # Автоматические действия при смене статуса
        if new_status == 'RLSE' and not self.release_date:
            self.release_date = timezone.now()
            self.set_stage('RELEASED')
        elif new_status == 'REJ':
            pass  # этап не меняем автоматически

        self.save()

        # Запись в историю
        StatusHistory.objects.create(
            cargo=self,
            old_status=old_status,
            new_status=new_status,
            changed_by=user,
            comment=comment,
        )
        logger.info(f'Груз {self.awb_number}: статус {old_status} → {new_status} (пользователь: {user})')

    def save(self, *args, **kwargs):
        # Правило: дата вылета недоступна на этапах DRAFT и FORMED
        if self.stage in ('DRAFT', 'FORMED'):
            self.departure_date = None

        # Правило: дата вылета ≤ дата прилёта
        if self.departure_date and self.flight_date:
            if self.departure_date > self.flight_date:
                raise ValueError(
                    f'Дата вылета ({self.departure_date}) не может быть позже '
                    f'даты прилёта ({self.flight_date})'
                )

        # Правило: дата прилёта ≤ дата размещения на СВХ
        if self.flight_date and self.scan_into_bond:
            if self.flight_date > self.scan_into_bond.date():
                raise ValueError(
                    f'Дата прилёта ({self.flight_date}) не может быть позже '
                    f'даты размещения на СВХ ({self.scan_into_bond.date()})'
                )

        # Правило: склад СВХ не может быть назначен на этапе «Формирование партии»
        if self.stage == 'DRAFT' and self.warehouse_id:
            logger.warning(
                f'Партия {self.awb_number}: попытка назначить склад на этапе '
                f'DRAFT — игнорируется (бизнес-правило)'
            )
            self.warehouse = None
            self.warehouse_name = ''
            self.warehouse_license = ''

        # Синхронизация warehouse_name и warehouse_license из связанного склада
        if self.warehouse:
            self.warehouse_name = self.warehouse.name
            self.warehouse_license = self.warehouse.license_number
        super().save(*args, **kwargs)


class StatusHistory(models.Model):
    """История изменения статусов груза"""
    cargo = models.ForeignKey(Cargo, on_delete=models.CASCADE,
                              verbose_name='Груз', related_name='status_history')
    old_status = models.CharField('Прежний статус', max_length=10, blank=True)
    new_status = models.CharField('Новый статус', max_length=10)
    changed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                                   verbose_name='Изменил')
    comment = models.TextField('Комментарий', blank=True)
    changed_at = models.DateTimeField('Время изменения', default=timezone.now)

    class Meta:
        verbose_name = 'История статуса'
        verbose_name_plural = 'История статусов'
        ordering = ['-changed_at']

    def __str__(self) -> str:
        return f'{self.cargo.awb_number}: {self.old_status}→{self.new_status} ({self.changed_at:%d.%m.%Y %H:%M})'


ROLE_CHOICES = [
    ('declarant',   'Декларант'),
    ('broker',      'Брокер'),
    ('manager',     'Менеджер'),
    ('supervisor',  'Супервайзер'),
    ('inspector',   'Инспектор'),
]


class CargoAssignment(models.Model):
    """Назначение сотрудников на груз"""
    cargo = models.ForeignKey(Cargo, on_delete=models.CASCADE,
                              verbose_name='Груз', related_name='assignments')
    user = models.ForeignKey(User, on_delete=models.CASCADE,
                             verbose_name='Сотрудник', related_name='cargo_assignments')
    role = models.CharField('Роль', max_length=20, choices=ROLE_CHOICES, default='declarant')
    assigned_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    verbose_name='Назначил', related_name='assigned_cargos')
    assigned_at = models.DateTimeField('Дата назначения', default=timezone.now)
    note = models.CharField('Примечание', max_length=200, blank=True)
    is_active = models.BooleanField('Активно', default=True)

    class Meta:
        verbose_name = 'Назначение сотрудника'
        verbose_name_plural = 'Назначения сотрудников'
        ordering = ['-assigned_at']
        unique_together = [('cargo', 'user', 'role')]

    def __str__(self) -> str:
        return f'{self.cargo.awb_number} — {self.get_role_display()}: {self.user.get_full_name() or self.user.username}'


# ─────────────────────────── ПЛАНИРОВАНИЕ НАГРУЗКИ ────────────────────────────

# Дефолтный график: ПН-ПТ 09:00-21:00, выходные пустые.
DEFAULT_WORK_SCHEDULE = {
    'mon': [['09:00', '21:00']],
    'tue': [['09:00', '21:00']],
    'wed': [['09:00', '21:00']],
    'thu': [['09:00', '21:00']],
    'fri': [['09:00', '21:00']],
    'sat': [],
    'sun': [],
}


class WorkScheduleException(models.Model):
    """Исключение из обычного графика — отпуск, больничный, праздник.
    На указанные даты capacity сотрудника = 0 (не учитывается в распределении).
    """
    KIND_CHOICES = [
        ('vacation', 'Отпуск'),
        ('sick',     'Больничный'),
        ('day_off',  'Отгул'),
        ('holiday',  'Праздник'),
        ('other',    'Иное'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE,
                             related_name='schedule_exceptions',
                             verbose_name='Сотрудник')
    date_from = models.DateField('Начало (включительно)')
    date_to = models.DateField('Окончание (включительно)')
    kind = models.CharField('Тип', max_length=20, choices=KIND_CHOICES, default='vacation')
    note = models.CharField('Комментарий', max_length=200, blank=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        verbose_name = 'Исключение из графика'
        verbose_name_plural = 'Исключения из графика'
        ordering = ['-date_from']
        indexes = [models.Index(fields=['user', 'date_from', 'date_to'])]

    def __str__(self) -> str:
        return f'{self.user.username}: {self.get_kind_display()} {self.date_from}—{self.date_to}'


class UserProfile(models.Model):
    """Расширение User — параметры сотрудника для планирования нагрузки."""
    user = models.OneToOneField(User, on_delete=models.CASCADE,
                                related_name='profile', verbose_name='Пользователь')
    timezone = models.CharField('Часовой пояс (IANA)', max_length=64, default='Europe/Moscow',
                                help_text='Например: Europe/Moscow, Asia/Vladivostok')
    is_active_op = models.BooleanField('Активный исполнитель', default=True,
                                       help_text='Учитывать в распределении нагрузки')
    primary_role = models.CharField('Основная роль', max_length=20,
                                    choices=ROLE_CHOICES, blank=True, default='')
    work_schedule = models.JSONField('График работы', default=dict, blank=True,
                                     help_text='{"mon":[["09:00","21:00"]], ..., "sun":[]}')
    daily_capacity_minutes = models.PositiveIntegerField(
        'Дневной лимит (минут)', default=0,
        help_text='0 — считать по графику, иначе override',
    )
    notes = models.CharField('Примечания', max_length=200, blank=True)

    class Meta:
        verbose_name = 'Профиль сотрудника'
        verbose_name_plural = 'Профили сотрудников'

    def __str__(self) -> str:
        return f'Профиль: {self.user.get_full_name() or self.user.username}'


class HouseWaybill(models.Model):
    """Индивидуальная накладная (HAWB) — независимая сущность, опционально привязана к MAWB"""

    CARGO_TYPE_CHOICES = [
        ('B2C', 'B2C — Бизнес для потребителя'),
        ('B2B', 'B2B — Бизнес для бизнеса'),
        ('C2C', 'C2C — Частное лицо'),
        ('DOC', 'Документация'),
    ]
    SHIPMENT_TYPE_CHOICES = [
        ('IMPORT', 'Импорт'),
        ('EXPORT', 'Экспорт'),
    ]

    # ── Связь с MAWB (необязательная) ──
    mawb = models.ForeignKey(Cargo, on_delete=models.SET_NULL,
                             null=True, blank=True,
                             verbose_name='Основная накладная (MAWB)',
                             related_name='hawbs')

    # ── Идентификация ──
    hawb_number = models.CharField('Номер HAWB', max_length=30, unique=True, db_index=True)
    description = models.TextField('Описание груза', blank=True)

    # ── Типы ──
    cargo_type    = models.CharField('Тип груза', max_length=5, choices=CARGO_TYPE_CHOICES, default='B2C')
    shipment_type = models.CharField('Тип отправки', max_length=10, choices=SHIPMENT_TYPE_CHOICES, default='IMPORT')

    # ── Получатель ──
    consignee_name    = models.CharField('Получатель', max_length=200, blank=True)
    consignee_address = models.TextField('Адрес доставки', blank=True)
    consignee_city    = models.CharField('Город', max_length=100, blank=True)
    consignee_phone   = models.CharField('Телефон', max_length=50, blank=True)
    consignee_email   = models.EmailField('Email', blank=True)
    consignee_inn     = models.CharField('ИНН получателя', max_length=20, blank=True)

    # ── Отправитель ──
    shipper_name    = models.CharField('Грузоотправитель', max_length=200, blank=True)
    shipper_inn     = models.CharField('ИНН отправителя', max_length=20, blank=True)
    shipper_city    = models.CharField('Город отправителя', max_length=100, blank=True)
    shipper_address = models.TextField('Адрес отправителя', blank=True)
    shipper_phone   = models.CharField('Телефон отправителя', max_length=50, blank=True)

    # ── Параметры ──
    weight          = models.DecimalField('Вес (кг)', max_digits=10, decimal_places=2, null=True, blank=True)
    pieces_declared = models.IntegerField('Мест', default=1)

    # ── Стоимость ──
    invoice_value    = models.DecimalField('Стоимость', max_digits=14, decimal_places=2, null=True, blank=True)
    invoice_currency = models.CharField('Валюта', max_length=3, choices=CURRENCY_CHOICES, default='USD')

    # ── Логистический статус ──
    LOGISTICS_STATUS_CHOICES = [
        # Подготовка
        ('CREATED',          'Создан'),
        ('TO_ORIGIN_WH',     'В пути на склад отправки'),
        ('AT_ORIGIN_WH',     'Принят на склад отправки'),
        ('CONSOLIDATED',     'Добавлен в консоль для отправки'),
        ('READY_TO_SHIP',    'Готов к отправке'),
        # Экспорт
        ('EXPORT_CUSTOMS',   'Экспортное таможенное оформление'),
        ('IN_TRANSIT_EXP',   'Груз в пути в страну назначения'),
        # Прибытие
        ('ARRIVED_DEST',     'Груз прибыл в страну назначения'),
        ('AT_SVH',           'Груз размещён на складе временного хранения'),
        # Импорт
        ('IMPORT_CUSTOMS',   'Таможенное оформление в стране назначения'),
        # После оформления
        ('READY_DELIVERY',   'Груз готов к отгрузке со СВХ'),
        ('TO_SORT_CENTER',   'Отгрузка со СВХ на сортировочный центр'),
        ('AT_SORT_CENTER',   'Приёмка на сортировочном центре'),
        ('READY_TO_DEST',    'Груз готов к отправке в пункт назначения'),
        ('IN_TRANSIT_DEST',  'Груз в пути в пункт назначения'),
        ('ARRIVED_FINAL',    'Груз прибыл в пункт назначения'),
        ('DELIVERED',        'Вручено'),
        # Нештатные
        ('RETURNED',         'Возврат отправителю'),
        ('LOST',             'Утеря груза'),
    ]

    # ── Таможенный статус (для EXPORT_CUSTOMS и IMPORT_CUSTOMS) ──
    CUSTOMS_STATUS_CHOICES = [
        ('',               '—'),
        ('BROKER_CHECK',   'Проверка брокером'),
        ('READY_TO_FILE',  'Готов к подаче'),
        ('FILED',          'Подан на таможенное оформление'),
        ('EXAMINATION',    'Досмотр'),
        ('HOLD',           'Удержан таможней'),
        ('RELEASED',       'Выпущен'),
        ('REJECTED',       'Отказ в выпуске'),
    ]

    # Статусы при которых активен таможенный статус
    CUSTOMS_ACTIVE_STATUSES = {'EXPORT_CUSTOMS', 'IMPORT_CUSTOMS'}

    # Статусы, физически невозможные без привязки к партии (MAWB)
    # Груз в этих статусах уже движется в составе партии или прибыл с ней.
    REQUIRES_MAWB_STATUSES = {
        'IN_TRANSIT_EXP', 'ARRIVED_DEST', 'AT_SVH', 'IMPORT_CUSTOMS',
        'READY_DELIVERY', 'TO_SORT_CENTER', 'AT_SORT_CENTER',
        'READY_TO_DEST', 'IN_TRANSIT_DEST', 'ARRIVED_FINAL', 'DELIVERED',
    }

    logistics_status      = models.CharField('Логистический статус', max_length=20,
                                             choices=LOGISTICS_STATUS_CHOICES,
                                             default='CREATED', db_index=True)
    logistics_status_date = models.DateTimeField('Дата лог. статуса', null=True, blank=True)

    customs_status      = models.CharField('Таможенный статус', max_length=20,
                                           choices=CUSTOMS_STATUS_CHOICES,
                                           blank=True, default='')
    customs_status_date = models.DateTimeField('Дата там. статуса', null=True, blank=True)

    last_status_change = models.DateTimeField('Изменение статуса', null=True, blank=True)

    # ── Ответственный сотрудник ──
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    verbose_name='Ответственный', related_name='hawb_assignments')
    ved_manager = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    verbose_name='Менеджер ВЭД', related_name='ved_managed_hawbs')

    # ── Поля, импортируемые из Google Sheets (таблица "Общее") ──
    problem_note = models.TextField('Проблема', blank=True,
                                    help_text='Описание текущей проблемы по накладной (колонка "Проблема" из Sheets)')
    tsd_number   = models.CharField('ТСД', max_length=64, blank=True,
                                    help_text='Номер транспортного сопроводительного документа')

    # ── Склад СВХ ──
    scan_into_bond = models.DateTimeField('Дата размещения на СВХ (ДО1)', null=True, blank=True)

    # ── Таможня ──
    customs_declaration_number = models.CharField('Номер ТД', max_length=50, blank=True)
    release_date = models.DateTimeField('Дата выпуска', null=True, blank=True)

    # ── Документы для таможенного оформления ──
    doc_invoice      = models.BooleanField('Инвойс', default=False)
    doc_packing_list = models.BooleanField('Упаковочный лист', default=False)
    doc_permit       = models.BooleanField('Разрешительные документы', default=False)
    doc_tech_desc    = models.BooleanField('Техническое описание', default=False)
    docs_required    = models.IntegerField('Требуется документов', default=4,
                                           help_text='Настраивается вручную — сколько документов нужно для подачи')

    # ── Системные ──
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлён', auto_now=True)
    notes = models.TextField('Примечания', blank=True)

    # ── Метки (теги для фильтрации) ──
    labels = models.ManyToManyField(Label, blank=True, related_name='hawbs',
                                    verbose_name='Метки')

    class Meta:
        verbose_name = 'Индивидуальная накладная (HAWB)'
        verbose_name_plural = 'Индивидуальные накладные (HAWB)'
        ordering = ['hawb_number']

    def __str__(self) -> str:
        mawb_part = self.mawb.awb_number if self.mawb else 'без партии'
        return f'HAWB {self.hawb_number} [{mawb_part}]'

    # ──────────────────────────────────────────
    # БИЗНЕС-ЛОГИКА (валидация и автоочистка)
    # ──────────────────────────────────────────

    # Статус, при котором накладная может быть добавлена в партию
    JOINABLE_STATUS = 'AT_ORIGIN_WH'

    @property
    def can_join_cargo(self) -> bool:
        """Накладная может быть добавлена в партию только в статусе 'Принят на склад отправки'"""
        return self.logistics_status == self.JOINABLE_STATUS

    @property
    def is_in_transit(self) -> bool:
        """Груз ещё в пути — не привязан к партии или у партии нет даты прилёта"""
        if not self.mawb:
            return True
        if self.mawb.flight_date and self.mawb.flight_date > timezone.now().date():
            return True
        return False

    @property
    def can_have_svh(self) -> bool:
        return not self.is_in_transit

    @property
    def can_have_dt_number(self) -> bool:
        return bool(self.mawb) and self.docs_ready

    @property
    def is_on_customs(self) -> bool:
        """Активно таможенное оформление"""
        return self.logistics_status in self.CUSTOMS_ACTIVE_STATUSES

    @property
    def is_export_customs(self) -> bool:
        return self.logistics_status == 'EXPORT_CUSTOMS'

    @property
    def is_import_customs(self) -> bool:
        return self.logistics_status == 'IMPORT_CUSTOMS'

    @property
    def logistics_status_display(self) -> str:
        return dict(self.LOGISTICS_STATUS_CHOICES).get(self.logistics_status, self.logistics_status)

    @property
    def customs_status_label(self) -> str:
        return dict(self.CUSTOMS_STATUS_CHOICES).get(self.customs_status, '')

    @property
    def full_status_display(self) -> str:
        base = self.logistics_status_display
        if self.customs_status and self.is_on_customs:
            return f'{base} → {self.customs_status_label}'
        return base

    @property
    def status_color(self) -> str:
        colors = {
            'CREATED':         'secondary',
            'TO_ORIGIN_WH':    'info',
            'AT_ORIGIN_WH':    'info',
            'CONSOLIDATED':    'primary',
            'READY_TO_SHIP':   'primary',
            'EXPORT_CUSTOMS':  'warning',
            'IN_TRANSIT_EXP':  'warning',
            'ARRIVED_DEST':    'warning',
            'AT_SVH':          'info',
            'IMPORT_CUSTOMS':  'warning',
            'READY_DELIVERY':  'primary',
            'TO_SORT_CENTER':  'primary',
            'AT_SORT_CENTER':  'primary',
            'READY_TO_DEST':   'primary',
            'IN_TRANSIT_DEST': 'warning',
            'ARRIVED_FINAL':   'success',
            'DELIVERED':       'success',
            'RETURNED':        'danger',
            'LOST':            'danger',
        }
        return colors.get(self.logistics_status, 'secondary')

    @property
    def customs_status_color(self) -> str:
        colors = {
            'BROKER_CHECK':  'info',
            'READY_TO_FILE': 'primary',
            'FILED':         'primary',
            'EXAMINATION':   'warning',
            'HOLD':          'danger',
            'RELEASED':      'success',
            'REJECTED':      'danger',
        }
        return colors.get(self.customs_status, 'secondary')

    def change_logistics_status(self, new_status: str, user=None, comment: str = '') -> str | None:
        """Смена логистического статуса с автоматической логикой. Возвращает текст ошибки или None при успехе."""
        # Правило: статус "В пути в страну назначения" требует привязки к партии (MAWB)
        if new_status == 'IN_TRANSIT_EXP' and not self.mawb_id:
            error = 'Невозможно установить статус «Груз в пути в страну назначения» без привязки к авианакладной (партии)'
            logger.warning(f'HAWB {self.hawb_number}: отказ смены лог.статуса — {error}')
            return error

        self.logistics_status = new_status
        self.logistics_status_date = timezone.now()
        self.last_status_change = timezone.now()

        # Автоматические действия
        if new_status == 'AT_SVH' and not self.scan_into_bond and self.mawb:
            self.scan_into_bond = timezone.now()

        if new_status in ('EXPORT_CUSTOMS', 'IMPORT_CUSTOMS') and not self.customs_status:
            self.customs_status = 'BROKER_CHECK'
            self.customs_status_date = timezone.now()

        if new_status == 'DELIVERED':
            self.release_date = self.release_date or timezone.now()

        if new_status == 'RETURNED':
            self.customs_status = ''

        self.save()
        logger.info(f'HAWB {self.hawb_number}: лог.статус → {new_status} ({user})')
        # Продвинуть активные воркфлоу
        from . import workflow_runner
        workflow_runner.advance_on_status_change(self, 'hawb', 'logistics_status', new_status)
        return None

    def change_customs_status(self, new_status: str, user=None) -> str | None:
        """Смена таможенного статуса. Возвращает текст ошибки или None при успехе."""

        # Правило: вес выпущенного груза не может превышать общий вес партии
        if new_status == 'RELEASED' and self.mawb and self.mawb.weight and self.weight:
            already_released = float(
                self.mawb.hawbs.filter(customs_status='RELEASED')
                .exclude(pk=self.pk)
                .aggregate(s=models.Sum('weight'))['s'] or 0
            )
            if already_released + float(self.weight) > float(self.mawb.weight) + 0.01:
                error = (
                    f'Невозможно выпустить: суммарный вес выпущенных накладных '
                    f'({already_released + float(self.weight):.2f} кг) превысит '
                    f'общий вес партии ({float(self.mawb.weight):.2f} кг)'
                )
                logger.warning(f'HAWB {self.hawb_number}: отказ в выпуске — {error}')
                return error

        self.customs_status = new_status
        self.customs_status_date = timezone.now()
        self.last_status_change = timezone.now()

        if new_status == 'RELEASED':
            self.release_date = self.release_date or timezone.now()
            # После выпуска на импорте — переводим в следующий лог.статус
            if self.logistics_status == 'IMPORT_CUSTOMS':
                self.logistics_status = 'READY_DELIVERY'
                self.logistics_status_date = timezone.now()
            elif self.logistics_status == 'EXPORT_CUSTOMS' and self.mawb_id:
                self.logistics_status = 'IN_TRANSIT_EXP'
                self.logistics_status_date = timezone.now()

        self.save()
        logger.info(f'HAWB {self.hawb_number}: там.статус → {new_status} ({user})')
        # Продвинуть активные воркфлоу
        from . import workflow_runner
        workflow_runner.advance_on_status_change(self, 'hawb', 'customs_status', new_status)
        return None

    @property
    def validation_errors(self) -> list:
        errors = []
        if self.scan_into_bond and not self.mawb:
            errors.append('Груз без партии не может иметь дату размещения на СВХ')
        if self.scan_into_bond and self.is_in_transit:
            errors.append('Груз в пути не может иметь дату размещения на СВХ')
        if self.customs_declaration_number and not self.mawb:
            errors.append('Груз без партии не может иметь регистрационный номер ДТ')
        if self.customs_declaration_number and not self.docs_ready:
            errors.append(f'Неполный чеклист ({self.docs_count}/{self.docs_required}) — рег. номер ДТ недоступен')
        return errors

    def save(self, *args, **kwargs):
        """Автоматически очищаем поля нарушающие бизнес-логику"""
        # Правило: накладная может быть добавлена в партию только в статусе AT_ORIGIN_WH
        if self.mawb_id:
            is_new_link = True
            if self.pk:
                old_mawb_id = HouseWaybill.objects.filter(pk=self.pk).values_list('mawb_id', flat=True).first()
                is_new_link = (old_mawb_id is None)
            if is_new_link and self.logistics_status != self.JOINABLE_STATUS:
                from django.core.exceptions import ValidationError
                raise ValidationError(
                    f'Накладная может быть добавлена в партию только в статусе '
                    f'«Принят на склад отправки» (AT_ORIGIN_WH). '
                    f'Текущий статус: {self.logistics_status_display}'
                )

        # Правило 0: без партии — статус не может быть "в пути/прибыл/на СВХ/оформление".
        # Такие статусы физически означают движение в составе партии.
        # Сценарий: партия была удалена (on_delete=SET_NULL) — откатываем в AT_ORIGIN_WH.
        if not self.mawb_id and self.logistics_status in self.REQUIRES_MAWB_STATUSES:
            logger.warning(
                f'HAWB {self.hawb_number}: автосброс статуса '
                f'{self.logistics_status} → AT_ORIGIN_WH (нет привязки к партии)'
            )
            self.logistics_status = 'AT_ORIGIN_WH'
            self.logistics_status_date = timezone.now()
            self.customs_status = ''
            self.customs_declaration_number = ''
            self.release_date = None

        # Правило 1 & 2: без партии или груз в пути — нет даты СВХ
        if not self.mawb or self.is_in_transit:
            self.scan_into_bond = None

        # Правило 3: нет даты СВХ — нет лицензии склада
        # (лицензия берётся из MAWB, поэтому просто убеждаемся что scan_into_bond чист)

        # Правило 4: неполный чеклист — нет рег. номера ДТ
        if self.customs_declaration_number and not self.docs_ready:
            self.customs_declaration_number = ''

        # Правило 5: не привязан к партии — нет рег. номера ДТ
        if self.customs_declaration_number and not self.mawb:
            self.customs_declaration_number = ''

        super().save(*args, **kwargs)

    @property
    def days_on_svh(self) -> int | None:
        """Дней на СВХ с момента размещения (ДО1)"""
        if not self.scan_into_bond:
            return None
        end = self.release_date or timezone.now()
        return (end - self.scan_into_bond).days

    @property
    def svh_timer_display(self) -> str:
        """Отображение таймера на СВХ"""
        if not self.scan_into_bond:
            return '—'
        total_hours = (timezone.now() - self.scan_into_bond).total_seconds() / 3600
        days = int(total_hours // 24)
        hours = int(total_hours % 24)
        if days > 0:
            return f'{days}д {hours}ч'
        return f'{int(total_hours)}ч'

    @property
    def svh_overdue(self) -> bool:
        """Более 7 дней на СВХ"""
        d = self.days_on_svh
        return d is not None and d > 7

    @property
    def docs_count(self) -> int:
        """Документов получено: из чеклиста (если заполнен) или старые флаги"""
        if self.pk:
            qs = self.checklist_items.all()
            if qs.exists():
                return qs.filter(is_received=True).count()
        return sum([self.doc_invoice, self.doc_packing_list,
                    self.doc_permit, self.doc_tech_desc])

    @property
    def docs_ready(self) -> bool:
        """Все требуемые документы получены"""
        if self.pk:
            qs = self.checklist_items.all()
            if qs.exists():
                required = qs.filter(is_required=True)
                if not required.exists():
                    return True
                return not required.filter(is_received=False).exists()
        return self.docs_count >= self.docs_required

    @property
    def hours_in_status(self) -> float | None:
        if not self.last_status_change:
            return None
        delta = timezone.now() - self.last_status_change
        return round(delta.total_seconds() / 3600, 1)

    @property
    def status_timer_display(self) -> str:
        hours = self.hours_in_status
        if hours is None:
            return '—'
        if hours < 1:
            return f'{int(hours * 60)}м'
        if hours < 24:
            h = int(hours)
            m = int((hours - h) * 60)
            return f'{h}ч {m}м' if m else f'{h}ч'
        days = int(hours // 24)
        rem_h = int(hours % 24)
        return f'{days}д {rem_h}ч' if rem_h else f'{days}д'

    @property
    def storage_start_date(self):
        """Дата начала хранения на СВХ"""
        return self.scan_into_bond

    @property
    def storage_end_date(self):
        """Дата окончания бесплатного хранения (120 дней по законодательству)"""
        if not self.scan_into_bond:
            return None
        from datetime import timedelta
        return self.scan_into_bond + timedelta(days=120)

    @property
    def storage_paid_start_date(self):
        """Дата начала платного хранения — дата ДО1 (scan_into_bond) + 4 дня"""
        if not self.scan_into_bond:
            return None
        from datetime import timedelta
        return self.scan_into_bond + timedelta(days=4)

    @property
    def storage_days_left(self):
        """Дней до окончания бесплатного хранения"""
        end = self.storage_end_date
        if not end:
            return None
        delta = end - timezone.now()
        return max(0, delta.days)

    @property
    def is_paid_storage(self):
        """Началось ли платное хранение"""
        paid = self.storage_paid_start_date
        if not paid:
            return False
        return timezone.now() >= paid

class HAWBGood(models.Model):
    """Товарная позиция внутри индивидуальной накладной HAWB"""
    hawb = models.ForeignKey(HouseWaybill, on_delete=models.CASCADE,
                             verbose_name='Накладная', related_name='goods')

    # ── Идентификация товара ──
    name            = models.CharField('Наименование товара', max_length=300)
    tnved_code      = models.CharField('Код ТН ВЭД', max_length=20, blank=True)
    brand           = models.CharField('Торговая марка', max_length=200, blank=True)
    manufacturer    = models.CharField('Изготовитель', max_length=200, blank=True)
    model           = models.CharField('Модель', max_length=200, blank=True)
    article         = models.CharField('Артикул', max_length=100, blank=True)
    # Для B2C товаров обязательна — URL карточки товара в интернет-магазине
    product_url     = models.URLField('Ссылка на товар', max_length=500, blank=True)

    # ── Количество и вес ──
    quantity        = models.DecimalField('Количество', max_digits=12, decimal_places=3, default=1)
    unit            = models.CharField('Единица измерения', max_length=20, default='шт')
    # ДЕИ — дополнительная единица измерения (для отдельных категорий ТН ВЭД).
    # Например: основная — упак, ДЕИ — 30 пар; основная — кг, ДЕИ — 5 м³
    quantity_additional = models.DecimalField('Кол-во в ДЕИ', max_digits=12, decimal_places=3, null=True, blank=True)
    unit_additional     = models.CharField('Доп. единица (ДЕИ)', max_length=20, blank=True)
    weight_net      = models.DecimalField('Вес нетто (кг)', max_digits=10, decimal_places=3, null=True, blank=True)
    weight_gross    = models.DecimalField('Вес брутто (кг)', max_digits=10, decimal_places=3, null=True, blank=True)

    # ── Стоимость ──
    unit_price      = models.DecimalField('Цена за единицу', max_digits=14, decimal_places=2, null=True, blank=True)
    total_value     = models.DecimalField('Общая стоимость', max_digits=14, decimal_places=2, null=True, blank=True)
    currency        = models.CharField('Валюта', max_length=3, choices=CURRENCY_CHOICES, default='USD')

    # ── Тип груза ──
    cargo_type      = models.CharField('Тип груза', max_length=5,
                                       choices=HouseWaybill.CARGO_TYPE_CHOICES,
                                       blank=True, default='')

    # ── Согласование ──
    APPROVAL_STATUS_CHOICES = [
        ('pending',       'На согласовании'),
        ('approved',      'Согласовано'),
        ('clarification', 'Требует уточнения'),
        ('rejected',      'Отклонено'),
    ]
    approval_status  = models.CharField('Статус согласования', max_length=20,
                                        choices=APPROVAL_STATUS_CHOICES, default='pending', db_index=True)
    approval_comment = models.TextField('Комментарий к согласованию', blank=True)
    approved_by      = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name='approved_goods', verbose_name='Согласовал')
    approved_at      = models.DateTimeField('Дата согласования', null=True, blank=True)

    # ── Системные ──
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Товарная позиция'
        verbose_name_plural = 'Товарные позиции'
        ordering = ['id']

    def __str__(self) -> str:
        return f'{self.hawb.hawb_number} — {self.name[:40]}'

    @property
    def approval_color(self) -> str:
        return {
            'pending':       'secondary',
            'approved':      'success',
            'clarification': 'warning',
            'rejected':      'danger',
        }.get(self.approval_status, 'secondary')

    @property
    def approval_label(self) -> str:
        return dict(self.APPROVAL_STATUS_CHOICES).get(self.approval_status, self.approval_status)

    @property
    def effective_cargo_type(self) -> str:
        """Тип груза: собственный, или унаследованный от родительской накладной"""
        return self.cargo_type or (self.hawb.cargo_type if self.hawb_id else '')

    def clean(self):
        super().clean()
        # Для B2C товаров обязательна ссылка на товар (URL страницы в интернет-магазине)
        if self.effective_cargo_type == 'B2C' and not self.product_url:
            from django.core.exceptions import ValidationError
            raise ValidationError({
                'product_url': 'Для B2C товаров обязательна ссылка на товар (URL страницы в интернет-магазине)'
            })

    def save(self, *args, **kwargs):
        if self.unit_price and self.quantity and not self.total_value:
            self.total_value = self.unit_price * self.quantity
        super().save(*args, **kwargs)


HAWB_CARGO_TYPE_CHOICES = [
    ('B2C', 'B2C — Бизнес для потребителя'),
    ('B2B', 'B2B — Бизнес для бизнеса'),
    ('C2C', 'C2C — Частное лицо'),
    ('DOC', 'Документация'),
]

DOCUMENT_CATEGORY_CHOICES = [
    ('transport',    'Транспортные документы'),
    ('commercial',   'Коммерческие документы'),
    ('customs',      'Таможенные документы'),
    ('permit',       'Разрешительные документы'),
    ('sanitary',     'Санитарные и ветеринарные'),
    ('certificate',  'Сертификаты и декларации'),
    ('other',        'Прочие'),
]


class DocumentType(models.Model):
    """Справочник типов документов для таможенного оформления"""
    name        = models.CharField('Название документа', max_length=300)
    description = models.TextField('Описание', blank=True)
    category    = models.CharField('Категория', max_length=20,
                                   choices=DOCUMENT_CATEGORY_CHOICES, default='other')
    is_active   = models.BooleanField('Активен', default=True)

    class Meta:
        verbose_name        = 'Тип документа'
        verbose_name_plural = 'Типы документов'
        ordering            = ['category', 'name']

    def __str__(self) -> str:
        return self.name


class CargoTypeDocTemplate(models.Model):
    """Шаблон обязательных документов для одной категории груза"""
    cargo_type = models.CharField('Категория груза', max_length=5, unique=True,
                                  choices=HAWB_CARGO_TYPE_CHOICES)

    class Meta:
        verbose_name        = 'Шаблон документов'
        verbose_name_plural = 'Шаблоны документов по категориям'
        ordering            = ['cargo_type']

    def __str__(self) -> str:
        return dict(HAWB_CARGO_TYPE_CHOICES).get(self.cargo_type, self.cargo_type)


class CargoCategoryDocRule(models.Model):
    """Правило: обязательные документы для категории груза (настраивается в админ-панели)"""
    template      = models.ForeignKey(CargoTypeDocTemplate, on_delete=models.CASCADE,
                                      verbose_name='Шаблон категории',
                                      related_name='rules',
                                      null=True, blank=True)
    cargo_type    = models.CharField('Категория груза', max_length=5,
                                     choices=HAWB_CARGO_TYPE_CHOICES)
    document_type = models.ForeignKey(DocumentType, on_delete=models.CASCADE,
                                      verbose_name='Тип документа',
                                      related_name='category_rules')

    class Meta:
        verbose_name        = 'Правило документов по категории'
        verbose_name_plural = 'Правила документов по категориям'
        unique_together     = [('cargo_type', 'document_type')]
        ordering            = ['cargo_type', 'document_type__name']

    def __str__(self) -> str:
        cat = dict(HAWB_CARGO_TYPE_CHOICES).get(self.cargo_type, self.cargo_type)
        return f'{cat}: {self.document_type.name}'


class HAWBChecklistItem(models.Model):
    """Элемент чеклиста документов для конкретной накладной"""
    hawb          = models.ForeignKey(HouseWaybill, on_delete=models.CASCADE,
                                      verbose_name='Накладная', related_name='checklist_items')
    document_type = models.ForeignKey(DocumentType, on_delete=models.PROTECT,
                                      verbose_name='Тип документа')
    is_required   = models.BooleanField('Обязательный', default=True,
                                        help_text='Обязателен для подачи ДТ по данной накладной')
    is_received   = models.BooleanField('Получен', default=False)
    notes         = models.TextField('Примечания', blank=True)
    added_by      = models.ForeignKey(User, on_delete=models.SET_NULL,
                                      null=True, blank=True,
                                      verbose_name='Добавил',
                                      related_name='added_checklist_items')
    created_at    = models.DateTimeField('Добавлен', auto_now_add=True)

    class Meta:
        verbose_name        = 'Элемент чеклиста'
        verbose_name_plural = 'Элементы чеклиста'
        unique_together     = [('hawb', 'document_type')]
        ordering            = ['-is_required', 'document_type__name']

    def __str__(self) -> str:
        mark = '✓' if self.is_received else '○'
        return f'{mark} {self.hawb.hawb_number}: {self.document_type.name}'


class HAWBDocument(models.Model):
    """Документ приложенный к индивидуальной накладной"""
    hawb = models.ForeignKey(HouseWaybill, on_delete=models.CASCADE,
                             verbose_name='Накладная', related_name='documents')

    DOC_TYPE_CHOICES = [
        ('invoice',      'Инвойс'),
        ('packing_list', 'Упаковочный лист'),
        ('permit',       'Разрешительный документ'),
        ('tech_desc',    'Техническое описание'),
        ('certificate',  'Сертификат'),
        ('declaration',  'Декларация'),
        ('other',        'Иное'),
    ]

    doc_type    = models.CharField('Тип документа', max_length=20, choices=DOC_TYPE_CHOICES, default='other')
    name        = models.CharField('Название документа', max_length=300)
    number      = models.CharField('Номер документа', max_length=100, blank=True)
    issue_date  = models.DateField('Дата выдачи', null=True, blank=True)
    is_received = models.BooleanField('Получен', default=False)
    notes       = models.TextField('Примечания', blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Документ HAWB'
        verbose_name_plural = 'Документы HAWB'
        ordering = ['doc_type', 'name']

    def __str__(self) -> str:
        return f'{self.hawb.hawb_number} — {self.get_doc_type_display()}: {self.name}'


# ─────────────────────────── DASHBOARD WIDGETS ───────────────────────────

class DashboardWidget(models.Model):
    WIDGET_TYPES = [
        ('stat',                 'Метрика'),
        ('kanban',               'Канбан'),
        ('table',                'Таблица'),
        ('chart_stage',          'График: этапы'),
        ('chart_warehouse',      'График: склады'),
        ('chart_pie',            'Круговая диаграмма'),
        ('forecast_arrivals',    'Прогноз прилётов'),
        ('pivot',                'Сводная таблица'),
        ('workload_heatmap',     'Тепловая карта нагрузки'),
        ('workload_my_day',      'Мой день'),
        ('workload_overloaded',  'Перегруженные'),
        ('workload_forecast',    'Прогноз нагрузки на 7 дней'),
    ]

    ENTITY_TYPES = [
        ('cargo', 'Партии (MAWB)'),
        ('hawb',  'Накладные (HAWB)'),
    ]

    user         = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dashboard_widgets')
    widget_type  = models.CharField('Тип', max_length=30, choices=WIDGET_TYPES, default='stat')
    entity_type  = models.CharField('Сущность', max_length=10, choices=ENTITY_TYPES, default='cargo')
    title        = models.CharField('Название', max_length=200)
    filter_query = models.TextField('Фильтр (CQL)', blank=True, default='')
    config       = models.JSONField('Конфигурация', default=dict, blank=True)
    pos_x        = models.IntegerField('Позиция X', default=0)
    pos_y        = models.IntegerField('Позиция Y', default=9999)
    width        = models.IntegerField('Ширина (колонки)', default=3)
    height       = models.IntegerField('Высота (строки)', default=2)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['pos_y', 'pos_x']
        verbose_name = 'Виджет дашборда'
        verbose_name_plural = 'Виджеты дашборда'

    def __str__(self):
        return f'{self.title} ({self.get_widget_type_display()}) — {self.user}'


# ─────────────────────────── WORKFLOWS ─────────────────────────────────

class Workflow(models.Model):
    """Бизнес-процесс (например «Импорт стандарт», «Возврат RTO»)."""

    ENTITY_TYPES = [
        ('cargo', 'Партия (MAWB)'),
        ('hawb',  'Накладная (HAWB)'),
    ]

    name        = models.CharField('Название', max_length=200)
    description = models.TextField('Описание', blank=True, default='')
    is_active   = models.BooleanField('Активен', default=True)
    # ── Привязка к типу сущности ──────────────────────────────────────────
    entity_type        = models.CharField('Тип сущности', max_length=10,
                                          choices=ENTITY_TYPES, default='cargo')
    trigger_conditions = models.JSONField('Условия запуска', default=dict, blank=True,
                                          help_text='JSON: {"cargo_type":"B2B","shipment_type":"IMPORT"}')
    auto_start         = models.BooleanField('Авто-старт при создании', default=True)
    # ─────────────────────────────────────────────────────────────────────
    created_by  = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='workflows_created')
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'Бизнес-процесс'
        verbose_name_plural = 'Бизнес-процессы'

    def __str__(self):
        return self.name


class WorkflowStep(models.Model):
    """Шаг (блок) бизнес-процесса."""
    STEP_TYPES = [
        ('stage',  'Этап партии'),       # привязан к STAGE_CHOICES
        ('action', 'Действие'),          # произвольное действие
        ('check',  'Проверка/условие'),  # условие ветвления
        ('notify', 'Уведомление'),       # авто-уведомление
    ]

    workflow  = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name='steps')
    name      = models.CharField('Название', max_length=200)
    step_type = models.CharField('Тип', max_length=20, choices=STEP_TYPES, default='stage')
    # Для Cargo-воркфлоу (entity_type='cargo')
    stage     = models.CharField('Привязка к этапу партии', max_length=20, choices=STAGE_CHOICES, blank=True, default='')
    # Для HAWB-воркфлоу (entity_type='hawb')
    hawb_logistics_status = models.CharField('Лог. статус накладной', max_length=20,
                                              choices=[
                                                  ('CREATED','Создан'),('TO_ORIGIN_WH','В пути на склад отправки'),
                                                  ('AT_ORIGIN_WH','Принят на склад отправки'),('CONSOLIDATED','В консоли'),
                                                  ('READY_TO_SHIP','Готов к отправке'),('EXPORT_CUSTOMS','Экспортная таможня'),
                                                  ('IN_TRANSIT_EXP','В пути в страну назначения'),
                                                  ('ARRIVED_DEST','Прибыл в страну назначения'),
                                                  ('AT_SVH','На СВХ'),('IMPORT_CUSTOMS','Импортная таможня'),
                                                  ('READY_DELIVERY','Готов к выдаче'),('TO_SORT_CENTER','На сортировку'),
                                                  ('AT_SORT_CENTER','На сортировочном центре'),
                                                  ('READY_TO_DEST','Готов к доставке'),
                                                  ('IN_TRANSIT_DEST','В пути к получателю'),
                                                  ('ARRIVED_FINAL','Прибыл к получателю'),
                                                  ('DELIVERED','Вручено'),('RETURNED','Возврат'),('LOST','Утеря'),
                                              ],
                                              blank=True, default='')
    hawb_customs_status   = models.CharField('Там. статус накладной', max_length=20,
                                              choices=[
                                                  ('BROKER_CHECK','Проверка брокером'),('READY_TO_FILE','Готов к подаче'),
                                                  ('FILED','Подан'),('EXAMINATION','Досмотр'),
                                                  ('HOLD','Удержан'),('RELEASED','Выпущен'),('REJECTED','Отказ'),
                                              ],
                                              blank=True, default='')
    config    = models.JSONField('Конфигурация', default=dict, blank=True)
    # Координаты на холсте (для визуального редактора)
    pos_x     = models.IntegerField('X на холсте', default=0)
    pos_y     = models.IntegerField('Y на холсте', default=0)
    color     = models.CharField('Цвет', max_length=20, default='#3b82f6')
    order     = models.IntegerField('Порядок', default=0)
    sla_hours_override = models.DecimalField(
        'SLA (часы), override', max_digits=7, decimal_places=2,
        null=True, blank=True,
        help_text='Если задано — перекрывает глобальный SLAPolicy для сущностей на этом шаге',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order']
        verbose_name = 'Шаг процесса'
        verbose_name_plural = 'Шаги процесса'

    def __str__(self):
        return f'{self.workflow.name} → {self.name}'


class WorkflowTransition(models.Model):
    """Переход (стрелка) между шагами."""
    workflow    = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name='transitions')
    from_step   = models.ForeignKey(WorkflowStep, on_delete=models.CASCADE, related_name='transitions_out')
    to_step     = models.ForeignKey(WorkflowStep, on_delete=models.CASCADE, related_name='transitions_in')
    label       = models.CharField('Подпись', max_length=200, blank=True, default='')
    condition   = models.TextField('Условие (CQL)', blank=True, default='')
    order       = models.IntegerField('Порядок', default=0)

    class Meta:
        ordering = ['order']
        verbose_name = 'Переход'
        verbose_name_plural = 'Переходы'

    def __str__(self):
        return f'{self.from_step.name} → {self.to_step.name}'


class AutomationRule(models.Model):
    """Правило автоматизации, срабатывающее на переходе."""
    ACTION_TYPES = [
        ('assign_user',       'Назначить пользователя'),
        ('set_field',         'Установить поле'),
        ('send_notify',       'Отправить уведомление'),
        ('change_stage',      'Перевести этап'),
        ('webhook',           'Вызвать webhook'),
        ('sla_breach_notify', 'Уведомить о просрочке SLA'),
    ]

    workflow    = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name='automations')
    transition  = models.ForeignKey(WorkflowTransition, on_delete=models.CASCADE, null=True, blank=True, related_name='automations')
    name        = models.CharField('Название', max_length=200)
    action_type = models.CharField('Тип действия', max_length=20, choices=ACTION_TYPES)
    config      = models.JSONField('Параметры', default=dict, blank=True)
    is_active   = models.BooleanField('Активно', default=True)
    order       = models.IntegerField('Порядок', default=0)

    class Meta:
        ordering = ['order']
        verbose_name = 'Правило автоматизации'
        verbose_name_plural = 'Правила автоматизации'

    def __str__(self):
        return f'{self.name} ({self.get_action_type_display()})'


class WorkflowInstance(models.Model):
    """Запущенный экземпляр бизнес-процесса для конкретной сущности."""

    STATUS_CHOICES = [
        ('active',    'Активен'),
        ('completed', 'Завершён'),
        ('cancelled', 'Отменён'),
    ]

    workflow     = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name='instances',
                                     verbose_name='Бизнес-процесс')
    entity_type  = models.CharField('Тип сущности', max_length=10,
                                    choices=[('cargo', 'Партия'), ('hawb', 'Накладная')])
    entity_id    = models.IntegerField('ID сущности', db_index=True)
    current_step = models.ForeignKey(WorkflowStep, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='active_instances',
                                     verbose_name='Текущий шаг')
    status       = models.CharField('Статус', max_length=20, choices=STATUS_CHOICES, default='active', db_index=True)
    started_at   = models.DateTimeField('Запущен', auto_now_add=True)
    completed_at = models.DateTimeField('Завершён', null=True, blank=True)
    started_by   = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='started_workflow_instances', verbose_name='Запущен пользователем')

    class Meta:
        unique_together = ('workflow', 'entity_type', 'entity_id')
        ordering = ['-started_at']
        verbose_name = 'Экземпляр бизнес-процесса'
        verbose_name_plural = 'Экземпляры бизнес-процессов'

    def __str__(self):
        return f'{self.workflow.name} / {self.entity_type}:{self.entity_id} [{self.status}]'


class WorkflowInstanceEvent(models.Model):
    """Событие перехода между шагами в рамках экземпляра бизнес-процесса."""

    instance       = models.ForeignKey(WorkflowInstance, on_delete=models.CASCADE,
                                       related_name='events', verbose_name='Экземпляр')
    from_step      = models.ForeignKey(WorkflowStep, on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name='events_from',
                                       verbose_name='Откуда')
    to_step        = models.ForeignKey(WorkflowStep, on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name='events_to',
                                       verbose_name='Куда')
    transition     = models.ForeignKey(WorkflowTransition, on_delete=models.SET_NULL,
                                       null=True, blank=True, verbose_name='Переход')
    triggered_by   = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                       verbose_name='Инициатор')
    triggered_at   = models.DateTimeField('Время', auto_now_add=True)
    note           = models.TextField('Комментарий', blank=True, default='')
    automation_log = models.JSONField('Лог автоматизаций', default=list,
                                      help_text='Список выполненных автоматизаций и их результатов')

    class Meta:
        ordering = ['triggered_at']
        verbose_name = 'Событие бизнес-процесса'
        verbose_name_plural = 'События бизнес-процессов'

    def __str__(self):
        return f'{self.instance} @ {self.triggered_at:%Y-%m-%d %H:%M}'


# ─────────────────────────── SLA ───────────────────────────

class SLAPolicy(models.Model):
    """Норматив времени на этапе/статусе сущности."""

    ENTITY_TYPES = [
        ('cargo', 'Партия'),
        ('hawb',  'Накладная'),
    ]
    STATUS_FIELDS = [
        ('stage',            'Этап партии'),
        ('logistics_status', 'Лог. статус накладной'),
        ('customs_status',   'Там. статус накладной'),
    ]

    name        = models.CharField('Название', max_length=200, blank=True, default='')
    entity_type = models.CharField('Сущность', max_length=10, choices=ENTITY_TYPES)
    status_field = models.CharField('Поле статуса', max_length=20, choices=STATUS_FIELDS)
    status_value = models.CharField('Значение статуса', max_length=30)
    hours        = models.DecimalField('Норматив (часы)', max_digits=7, decimal_places=2)
    warning_threshold_pct = models.IntegerField(
        'Порог жёлтой зоны, %', default=75,
        help_text='При каком проценте израсходованного времени полоса становится жёлтой',
    )
    is_active    = models.BooleanField('Активно', default=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('entity_type', 'status_field', 'status_value')
        ordering = ['entity_type', 'status_field', 'status_value']
        verbose_name = 'SLA-норматив'
        verbose_name_plural = 'SLA-нормативы'

    def __str__(self):
        return f'{self.get_entity_type_display()} / {self.status_value} → {self.hours}ч'


class SLABreachEvent(models.Model):
    """Событие просрочки SLA — пишется management-командой, обеспечивает идемпотентность уведомлений."""

    policy      = models.ForeignKey(SLAPolicy, on_delete=models.CASCADE, related_name='breaches')
    entity_type = models.CharField('Тип сущности', max_length=10, db_index=True)
    entity_id   = models.IntegerField('ID сущности', db_index=True)
    breached_at = models.DateTimeField('Момент просрочки')
    notified    = models.BooleanField('Уведомление отправлено', default=False)
    workflow_instance = models.ForeignKey(
        WorkflowInstance, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sla_breaches',
    )
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('policy', 'entity_type', 'entity_id', 'breached_at')
        ordering = ['-breached_at']
        verbose_name = 'Событие просрочки SLA'
        verbose_name_plural = 'События просрочки SLA'

    def __str__(self):
        return f'Breach {self.policy_id} {self.entity_type}#{self.entity_id} @ {self.breached_at:%Y-%m-%d %H:%M}'


# ─────────────────────────── НОРМАТИВЫ И ЛОГ ПЕРЕРАСПРЕДЕЛЕНИЯ ─────────────────

class ProcessingNorm(models.Model):
    """Норматив времени на обработку одной HAWB по типу отправки и категории груза."""
    shipment_type = models.CharField(
        'Тип отправки', max_length=10,
        choices=HouseWaybill.SHIPMENT_TYPE_CHOICES,
    )
    cargo_type = models.CharField(
        'Тип груза', max_length=5,
        choices=HouseWaybill.CARGO_TYPE_CHOICES,
    )
    minutes = models.PositiveIntegerField(
        'Норматив (минут)',
        help_text='Среднее время обработки одной накладной этого типа',
    )
    is_active = models.BooleanField('Активен', default=True)
    updated_at = models.DateTimeField('Обновлён', auto_now=True)

    class Meta:
        verbose_name = 'Норматив обработки'
        verbose_name_plural = 'Нормативы обработки'
        unique_together = [('shipment_type', 'cargo_type')]
        ordering = ['shipment_type', 'cargo_type']

    def __str__(self) -> str:
        return f'{self.shipment_type} × {self.cargo_type} → {self.minutes} мин'


class OrganizationSettings(models.Model):
    """Singleton: реквизиты организации-получателя для печатных форм (ДО1, манифест и т.п.)."""
    name = models.CharField('Название организации', max_length=200, blank=True)
    inn = models.CharField('ИНН', max_length=20, blank=True)
    ogrn = models.CharField('ОГРН', max_length=20, blank=True)
    bank_account = models.CharField('Расчётный счёт (Р/с)', max_length=30, blank=True)
    bank_name = models.CharField('Банк (филиал)', max_length=200, blank=True)
    bank_corr_account = models.CharField('Корреспондентский счёт (К/с)', max_length=30, blank=True)
    bank_bik = models.CharField('БИК', max_length=20, blank=True)
    manifest_shipper = models.CharField(
        'Грузоотправитель в манифесте', max_length=200, blank=True,
        help_text='Заполняется в столбец «Грузоотправитель» во всех строках манифеста',
    )
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Настройки организации'
        verbose_name_plural = 'Настройки организации'

    def __str__(self) -> str:
        return self.name or 'Организация (не задана)'

    @classmethod
    def get_solo(cls) -> 'OrganizationSettings':
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class WorkloadRebalanceLog(models.Model):
    """Лог перераспределения накладных между сотрудниками."""
    REASON_CHOICES = [
        ('overload_manual', 'Ручное перераспределение перегруза'),
        ('overload_auto',   'Автоматическое перераспределение'),
    ]

    hawb = models.ForeignKey(HouseWaybill, on_delete=models.CASCADE,
                             related_name='rebalance_logs', verbose_name='Накладная')
    from_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='rebalance_from', verbose_name='Откуда')
    to_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='rebalance_to', verbose_name='Куда')
    reason = models.CharField('Причина', max_length=30, choices=REASON_CHOICES,
                              default='overload_manual')
    target_date = models.DateField('Целевая дата', null=True, blank=True,
                                   help_text='День, на который вычислялась нагрузка')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='rebalance_actions',
                                   verbose_name='Кто инициировал')
    created_at = models.DateTimeField('Время', auto_now_add=True)

    class Meta:
        verbose_name = 'Перераспределение нагрузки'
        verbose_name_plural = 'Лог перераспределения нагрузки'
        ordering = ['-created_at']

    def __str__(self) -> str:
        f = self.from_user.username if self.from_user else '?'
        t = self.to_user.username if self.to_user else '?'
        return f'{self.hawb_id}: {f} → {t} ({self.created_at:%d.%m %H:%M})'


# ─────────────────────────── ОЧЕРЕДЬ ОТПРАВКИ В АЛЬТУ ───────────────────────────

class AltaQueueItem(models.Model):
    """Документ, ожидающий выгрузки в hot-folder Альты через мини-агент.

    Django сам не имеет доступа к ПК с Альтой, поэтому Django кладёт XML сюда,
    а сторонний скрипт-агент опрашивает API /api/v1/alta/queue/, скачивает
    файлы и кладёт их в hot-folder локальной Альты.
    """
    DOC_TYPE_CHOICES = [
        ('indpost',  'Накладная'),
        ('waybill',  'Накладная (ЭД-2)'),
        ('invoice',  'Инвойс'),
        ('express',  'Реестр экспресс-грузов'),
        ('dt',       'Декларация на товары'),
    ]
    STATUS_CHOICES = [
        ('pending', 'В очереди'),
        ('sent',    'Отправлено'),
        ('failed',  'Ошибка'),
    ]

    doc_type = models.CharField('Тип документа', max_length=16, choices=DOC_TYPE_CHOICES, db_index=True)

    # FK на исходную сущность (одно из двух). Не строго required — Альта может
    # переварить документ, даже если запись в БД удалят.
    hawb  = models.ForeignKey(HouseWaybill, on_delete=models.SET_NULL,
                              null=True, blank=True, related_name='alta_queue_items',
                              verbose_name='HAWB')
    cargo = models.ForeignKey(Cargo, on_delete=models.SET_NULL,
                              null=True, blank=True, related_name='alta_queue_items',
                              verbose_name='Партия (MAWB)')

    filename = models.CharField('Имя файла', max_length=255)
    content  = models.BinaryField('XML (байты в исходной кодировке)')
    content_encoding = models.CharField('Кодировка', max_length=32, default='utf-8')

    status = models.CharField('Статус', max_length=10, choices=STATUS_CHOICES,
                              default='pending', db_index=True)
    error_message = models.TextField('Сообщение об ошибке', blank=True)
    retry_count   = models.IntegerField('Попыток отправки', default=0)

    # UUID из <roi:EnvelopeID> в content. Заполняется при enqueue.
    # Используется для матчинга входящих ЭД (AltaInboxMessage.parsed_meta.initial_envelope
    # → этот envelope_id → hawb). Nullable для старых записей до миграции.
    envelope_id = models.CharField('Envelope ID (UUID)', max_length=64,
                                   unique=True, null=True, blank=True, db_index=True)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='alta_queue_items', verbose_name='Кто поставил')
    created_at = models.DateTimeField('Создан', auto_now_add=True, db_index=True)
    sent_at    = models.DateTimeField('Отправлен', null=True, blank=True)

    class Meta:
        verbose_name = 'Документ в очереди на Альту'
        verbose_name_plural = 'Очередь отправки в Альту'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'created_at']),
        ]

    def __str__(self) -> str:
        return f'[{self.get_status_display()}] {self.get_doc_type_display()} — {self.filename}'

    def mark_sent(self) -> None:
        self.status = 'sent'
        self.sent_at = timezone.now()
        self.error_message = ''
        self.save(update_fields=['status', 'sent_at', 'error_message'])

    def mark_failed(self, message: str) -> None:
        self.status = 'failed'
        self.error_message = (message or '')[:5000]
        self.retry_count = (self.retry_count or 0) + 1
        self.save(update_fields=['status', 'error_message', 'retry_count'])


class AltaInboxMessage(models.Model):
    """Входящее ЭД-сообщение от таможни, прилетевшее через alta_agent inbox-thread.

    Парный к AltaQueueItem: туда мы кладём исходящее, оттуда читаем входящее.
    Источник — gzip из C:\\GTDSERV\\ED\\IN на рабочей виртуалке; собирает
    inbox_loop в alta_agent.py, шлёт POST /api/v1/alta/inbox/ на VPS.
    """
    KIND_CHOICES = [
        ('registered',  'Регистрация ДТ'),
        ('released',    'Выпуск ДТ'),
        ('rejected',    'Отказ в выпуске'),
        ('examination', 'Досмотр / запрос'),
        ('hold',        'Требование / арест'),
        ('info',        'Информационное'),
    ]

    envelope_id = models.CharField('Envelope ID (Альта)', max_length=64,
                                   unique=True, db_index=True)
    msg_type    = models.CharField('MessageType (ED.xxx)', max_length=32, db_index=True)
    msg_kind    = models.CharField('Semantic kind', max_length=16,
                                   choices=KIND_CHOICES, default='info', db_index=True)

    waybill_number_raw = models.CharField('WayBillNumber из XML',
                                          max_length=64, blank=True, db_index=True)
    declaration_number = models.CharField('№ ДТ из XML',
                                          max_length=64, blank=True, db_index=True)

    prepared_at = models.DateTimeField('Время отправки (PreparationDateTime)',
                                       null=True, blank=True)
    received_at = models.DateTimeField('Записано в БД', auto_now_add=True, db_index=True)

    raw_xml     = models.TextField('Полный XML (для аудита)', blank=True)
    parsed_meta = models.JSONField('Распаршенные поля', default=dict, blank=True)

    hawb = models.ForeignKey(HouseWaybill, on_delete=models.SET_NULL,
                             null=True, blank=True, related_name='inbox_messages',
                             verbose_name='HAWB (сматчена)')
    status_applied = models.BooleanField('Статус применён', default=False, db_index=True)

    class Meta:
        verbose_name = 'Сообщение от таможни'
        verbose_name_plural = 'Сообщения от таможни (входящие)'
        ordering = ['-received_at']
        indexes = [
            models.Index(fields=['msg_kind', '-received_at']),
            models.Index(fields=['hawb', '-received_at']),
        ]

    def __str__(self) -> str:
        return f'[{self.get_msg_kind_display()}] {self.msg_type} — {self.envelope_id}'


# ─────────────────────────── ИМПОРТ ИЗ GOOGLE SHEETS ───────────────────────────

class SheetSource(models.Model):
    """Конфигурация источника импорта из Google Sheets — одна вкладка одного spreadsheet'а."""
    KIND_CHOICES = [
        ('general', 'Таблица "Общее"'),
        ('crm',     'Таблица CRM'),
    ]
    STATUS_CHOICES = [
        ('',      '—'),
        ('ok',    'OK'),
        ('error', 'Ошибка'),
    ]

    name           = models.CharField('Название', max_length=100,
                                      help_text='Произвольное имя для отображения в UI')
    kind           = models.CharField('Тип источника', max_length=20, choices=KIND_CHOICES, db_index=True)
    spreadsheet_id = models.CharField('Spreadsheet ID', max_length=128,
                                      help_text='ID из URL Google-таблицы')
    tab_name       = models.CharField('Имя вкладки', max_length=128)
    header_row     = models.PositiveIntegerField('Номер строки шапки', default=1,
                                                 help_text='1-based номер строки с заголовками колонок')
    is_active      = models.BooleanField('Активен', default=True)

    last_imported_at = models.DateTimeField('Последний импорт', null=True, blank=True)
    last_status      = models.CharField('Последний статус', max_length=10, choices=STATUS_CHOICES, blank=True)
    last_error       = models.TextField('Последняя ошибка', blank=True)
    notes            = models.TextField('Заметки', blank=True)

    class Meta:
        verbose_name = 'Источник Google Sheets'
        verbose_name_plural = 'Источники Google Sheets'
        unique_together = [('spreadsheet_id', 'tab_name')]
        ordering = ['kind', 'name']

    def __str__(self) -> str:
        return f'{self.get_kind_display()} → {self.name}'


class ImportedSheetRow(models.Model):
    """Сырая строка из Sheets + результат матчинга с HouseWaybill."""
    MATCH_STATUS_CHOICES = [
        ('unmatched', 'Не обработано'),
        ('matched',   'Совпадает с HAWB'),
        ('orphan',    'Нет соответствия в БД'),
        ('conflict',  'Несколько кандидатов'),
        ('ambiguous', 'Нет ключа для матчинга'),
        ('promoted',  'Промоутнуто в HAWB'),
        ('ignored',   'Исключено вручную'),
    ]

    source           = models.ForeignKey(SheetSource, on_delete=models.CASCADE, related_name='rows')
    source_row_index = models.PositiveIntegerField('№ строки в Sheets', db_index=True)
    data             = models.JSONField('Сырые данные', default=dict)
    content_hash     = models.CharField('Hash контента', max_length=64, db_index=True)

    # Извлечённые ключи — для индексации и поиска
    hawb_number_raw    = models.CharField('HAWB (как в Sheets)', max_length=64, blank=True, db_index=True)
    hawb_number_norm   = models.CharField('HAWB (нормализованный)', max_length=64, blank=True, db_index=True)
    inn_raw            = models.CharField('ИНН (как в Sheets)', max_length=32, blank=True, db_index=True)
    declaration_number = models.CharField('№ ДТ', max_length=64, blank=True, db_index=True)
    arrival_date       = models.DateField('Дата прибытия (из Sheets)', null=True, blank=True, db_index=True)

    match_status  = models.CharField('Статус матчинга', max_length=20, choices=MATCH_STATUS_CHOICES,
                                     default='unmatched', db_index=True)
    matched_hawb  = models.ForeignKey(HouseWaybill, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name='imported_rows', verbose_name='Сматченная HAWB')
    matched_cargo = models.ForeignKey(Cargo, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name='imported_rows', verbose_name='Сматченная партия')
    promoted_hawb = models.ForeignKey(HouseWaybill, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name='promoted_from_rows', verbose_name='Создана HAWB')

    diff_summary = models.JSONField('Расхождения с БД', default=dict, blank=True)

    first_seen_at    = models.DateTimeField('Впервые увидели', auto_now_add=True)
    last_seen_at     = models.DateTimeField('Последнее обновление записи', auto_now=True)
    last_imported_at = models.DateTimeField('Последний прогон импорта', default=timezone.now)

    class Meta:
        verbose_name = 'Строка из Sheets'
        verbose_name_plural = 'Строки из Sheets'
        unique_together = [('source', 'source_row_index')]
        indexes = [
            models.Index(fields=['match_status', 'source']),
            models.Index(fields=['hawb_number_norm']),
        ]
        ordering = ['source', 'source_row_index']

    def __str__(self) -> str:
        return f'{self.source.name}#{self.source_row_index} [{self.get_match_status_display()}]'


class SheetImportRun(models.Model):
    """Журнал одного прогона импорта."""
    STATUS_CHOICES = [
        ('running', 'В процессе'),
        ('ok',      'Успешно'),
        ('error',   'Ошибка'),
    ]

    source     = models.ForeignKey(SheetSource, on_delete=models.CASCADE, related_name='runs')
    started_at = models.DateTimeField('Начало', auto_now_add=True, db_index=True)
    finished_at= models.DateTimeField('Завершение', null=True, blank=True)
    status     = models.CharField('Статус', max_length=10, choices=STATUS_CHOICES, default='running')

    rows_total     = models.PositiveIntegerField('Всего строк', default=0)
    rows_new       = models.PositiveIntegerField('Новых', default=0)
    rows_changed   = models.PositiveIntegerField('Изменившихся', default=0)
    rows_unchanged = models.PositiveIntegerField('Без изменений', default=0)
    rows_matched   = models.PositiveIntegerField('Сматчены', default=0)
    rows_orphan    = models.PositiveIntegerField('Orphan', default=0)
    rows_conflict  = models.PositiveIntegerField('Конфликты', default=0)

    error_message = models.TextField('Сообщение об ошибке', blank=True)
    triggered_by  = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name='sheet_import_runs')
    dry_run       = models.BooleanField('Dry-run', default=False)

    class Meta:
        verbose_name = 'Прогон импорта'
        verbose_name_plural = 'Прогоны импорта'
        ordering = ['-started_at']

    def __str__(self) -> str:
        return f'{self.source.name} {self.started_at:%d.%m %H:%M} [{self.get_status_display()}]'


class SheetUserAlias(models.Model):
    """Маппинг ФИО как в Sheets → User в нашей БД."""
    ROLE_HINTS = [
        ('',             '—'),
        ('ved_manager',  'Менеджер ВЭД'),
        ('declarant',    'Декларант / ответственный по ТО'),
        ('broker',       'Брокер'),
        ('manager',      'Менеджер'),
    ]

    alias     = models.CharField('ФИО как в Sheets', max_length=200, unique=True)
    user      = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sheet_aliases')
    role_hint = models.CharField('Роль (подсказка)', max_length=20, choices=ROLE_HINTS, blank=True)
    notes     = models.TextField('Заметки', blank=True)

    class Meta:
        verbose_name = 'Алиас пользователя из Sheets'
        verbose_name_plural = 'Алиасы пользователей из Sheets'
        ordering = ['alias']

    def __str__(self) -> str:
        return f'{self.alias} → {self.user.username}'


class HawbWorkflowEvent(models.Model):
    """Событие в таймлайне HAWB (этап workflow).

    Каждая колонка CRM-таблицы Sheets, означающая «когда случилось такое-то событие»,
    материализуется как одна запись этого типа. Из набора событий можно восстановить
    любую дату любого этапа без дублирования полей в HouseWaybill.
    """
    EVENT_TYPES = [
        ('TZ_AGREED',          'ТЗ согласовано'),
        ('GOV_CONTROL',        'Госконтроль'),
        ('VED_DOCS_COLLECTED', 'ВЭД собрал документы'),
        ('DOCS_PROVIDED',      'Документы предоставлены'),
        ('DOCS_REQUESTED',     'Запрос документов'),
        ('DOCS_RESPONSE',      'Ответ на запрос документов'),
        ('CALC_SENT',          'Отправлен расчёт ТП'),
        ('PAYMENT_DONE',       'Оплата счёта'),
        ('READY_TO_FILE',      'Готово к подаче'),
        ('FILED_FOR_CUSTOMS',  'Подано на ТО'),
        ('CUSTOMS_REQUEST',    'Запрос таможни'),
        ('VED_RESPONSE',       'Ответ ВЭДа по запросу таможни'),
        ('DECLARATION_ISSUED', '№ декларации на выпуск'),
        ('OTHER',              'Прочее'),
    ]
    SOURCE_CHOICES = [
        ('sheet', 'Из Google Sheets'),
        ('user',  'Вручную'),
        ('api',   'Внешний API'),
    ]

    hawb        = models.ForeignKey(HouseWaybill, on_delete=models.CASCADE,
                                    related_name='workflow_events')
    event_type  = models.CharField('Тип события', max_length=32, choices=EVENT_TYPES, db_index=True)
    occurred_at = models.DateTimeField('Когда произошло', db_index=True)
    comment     = models.TextField('Комментарий', blank=True)
    raw_value   = models.CharField('Исходное значение', max_length=255, blank=True)

    set_by      = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='set_workflow_events')
    source      = models.CharField('Источник', max_length=10, choices=SOURCE_CHOICES, default='sheet')
    source_row  = models.ForeignKey(ImportedSheetRow, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name='emitted_events')

    created_at  = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        verbose_name = 'Событие workflow HAWB'
        verbose_name_plural = 'События workflow HAWB'
        unique_together = [('hawb', 'event_type', 'source_row')]
        indexes = [models.Index(fields=['hawb', 'event_type', 'occurred_at'])]
        ordering = ['-occurred_at']

    def __str__(self) -> str:
        return f'{self.hawb.hawb_number} · {self.get_event_type_display()} · {self.occurred_at:%d.%m.%Y}'
