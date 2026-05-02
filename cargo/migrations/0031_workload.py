"""Миграция: модели планирования нагрузки сотрудников.

Добавляет:
  * UserProfile — параметры сотрудника (таймзона, график, лимит).
  * ProcessingNorm — норматив минут на одну HAWB по (shipment_type, cargo_type).
  * WorkloadRebalanceLog — лог перераспределений между сотрудниками.
  * Расширение DashboardWidget.WIDGET_TYPES (4 новых типа виджета).

Data-migration: создаёт 8 дефолтных нормативов и UserProfile для всех существующих
пользователей с дефолтным графиком ПН-ПТ 09:00-21:00 в Europe/Moscow.
"""
from django.conf import settings
from django.db import migrations, models


# ── Дефолтные значения для data-migration ─────────────────────────────────────

DEFAULT_NORMS = [
    # (shipment_type, cargo_type, minutes)
    ('IMPORT', 'B2C', 30),
    ('IMPORT', 'B2B', 90),
    ('IMPORT', 'C2C', 45),
    ('IMPORT', 'DOC', 15),
    ('EXPORT', 'B2C', 25),
    ('EXPORT', 'B2B', 75),
    ('EXPORT', 'C2C', 40),
    ('EXPORT', 'DOC', 10),
]

DEFAULT_SCHEDULE = {
    'mon': [['09:00', '21:00']],
    'tue': [['09:00', '21:00']],
    'wed': [['09:00', '21:00']],
    'thu': [['09:00', '21:00']],
    'fri': [['09:00', '21:00']],
    'sat': [],
    'sun': [],
}


def seed_norms_and_profiles(apps, schema_editor):
    ProcessingNorm = apps.get_model('cargo', 'ProcessingNorm')
    UserProfile = apps.get_model('cargo', 'UserProfile')
    User = apps.get_model(settings.AUTH_USER_MODEL.split('.')[0],
                          settings.AUTH_USER_MODEL.split('.')[1])

    for shipment_type, cargo_type, minutes in DEFAULT_NORMS:
        ProcessingNorm.objects.update_or_create(
            shipment_type=shipment_type,
            cargo_type=cargo_type,
            defaults={'minutes': minutes, 'is_active': True},
        )

    for user in User.objects.all():
        UserProfile.objects.get_or_create(
            user=user,
            defaults={
                'timezone': 'Europe/Moscow',
                'is_active_op': bool(user.is_active),
                'work_schedule': DEFAULT_SCHEDULE,
            },
        )


def reverse_noop(apps, schema_editor):
    """Откат не удаляет данные — модели всё равно сносятся следующим оператором."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0030_remove_cargo_aca_location_remove_cargo_queue_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── Расширение WIDGET_TYPES (без миграции схемы — choices только для админки/формы) ──
        migrations.AlterField(
            model_name='dashboardwidget',
            name='widget_type',
            field=models.CharField(
                choices=[
                    ('stat',                'Метрика'),
                    ('kanban',              'Канбан'),
                    ('table',               'Таблица'),
                    ('chart_stage',         'График: этапы'),
                    ('chart_warehouse',     'График: склады'),
                    ('chart_pie',           'Круговая диаграмма'),
                    ('forecast_arrivals',   'Прогноз прилётов'),
                    ('pivot',               'Сводная таблица'),
                    ('workload_heatmap',    'Тепловая карта нагрузки'),
                    ('workload_my_day',     'Мой день'),
                    ('workload_overloaded', 'Перегруженные'),
                    ('workload_forecast',   'Прогноз нагрузки на 7 дней'),
                ],
                default='stat',
                max_length=30,
                verbose_name='Тип',
            ),
        ),

        # ── UserProfile ──
        migrations.CreateModel(
            name='UserProfile',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('timezone', models.CharField(default='Europe/Moscow', help_text='Например: Europe/Moscow, Asia/Vladivostok', max_length=64, verbose_name='Часовой пояс (IANA)')),
                ('is_active_op', models.BooleanField(default=True, help_text='Учитывать в распределении нагрузки', verbose_name='Активный исполнитель')),
                ('primary_role', models.CharField(blank=True, choices=[('declarant', 'Декларант'), ('broker', 'Брокер'), ('manager', 'Менеджер'), ('supervisor', 'Супервайзер'), ('inspector', 'Инспектор')], default='', max_length=20, verbose_name='Основная роль')),
                ('work_schedule', models.JSONField(blank=True, default=dict, help_text='{"mon":[["09:00","21:00"]], ..., "sun":[]}', verbose_name='График работы')),
                ('daily_capacity_minutes', models.PositiveIntegerField(default=0, help_text='0 — считать по графику, иначе override', verbose_name='Дневной лимит (минут)')),
                ('notes', models.CharField(blank=True, max_length=200, verbose_name='Примечания')),
                ('user', models.OneToOneField(on_delete=models.deletion.CASCADE, related_name='profile', to=settings.AUTH_USER_MODEL, verbose_name='Пользователь')),
            ],
            options={
                'verbose_name': 'Профиль сотрудника',
                'verbose_name_plural': 'Профили сотрудников',
            },
        ),

        # ── ProcessingNorm ──
        migrations.CreateModel(
            name='ProcessingNorm',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('shipment_type', models.CharField(choices=[('IMPORT', 'Импорт'), ('EXPORT', 'Экспорт')], max_length=10, verbose_name='Тип отправки')),
                ('cargo_type', models.CharField(choices=[('B2C', 'B2C — Бизнес для потребителя'), ('B2B', 'B2B — Бизнес для бизнеса'), ('C2C', 'C2C — Частное лицо'), ('DOC', 'Документация')], max_length=5, verbose_name='Тип груза')),
                ('minutes', models.PositiveIntegerField(help_text='Среднее время обработки одной накладной этого типа', verbose_name='Норматив (минут)')),
                ('is_active', models.BooleanField(default=True, verbose_name='Активен')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлён')),
            ],
            options={
                'verbose_name': 'Норматив обработки',
                'verbose_name_plural': 'Нормативы обработки',
                'ordering': ['shipment_type', 'cargo_type'],
                'unique_together': {('shipment_type', 'cargo_type')},
            },
        ),

        # ── WorkloadRebalanceLog ──
        migrations.CreateModel(
            name='WorkloadRebalanceLog',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('reason', models.CharField(choices=[('overload_manual', 'Ручное перераспределение перегруза'), ('overload_auto', 'Автоматическое перераспределение')], default='overload_manual', max_length=30, verbose_name='Причина')),
                ('target_date', models.DateField(blank=True, help_text='День, на который вычислялась нагрузка', null=True, verbose_name='Целевая дата')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Время')),
                ('hawb', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='rebalance_logs', to='cargo.housewaybill', verbose_name='Накладная')),
                ('from_user', models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name='rebalance_from', to=settings.AUTH_USER_MODEL, verbose_name='Откуда')),
                ('to_user', models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name='rebalance_to', to=settings.AUTH_USER_MODEL, verbose_name='Куда')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name='rebalance_actions', to=settings.AUTH_USER_MODEL, verbose_name='Кто инициировал')),
            ],
            options={
                'verbose_name': 'Перераспределение нагрузки',
                'verbose_name_plural': 'Лог перераспределения нагрузки',
                'ordering': ['-created_at'],
            },
        ),

        # ── Data: дефолтные нормативы и профили ──
        migrations.RunPython(seed_norms_and_profiles, reverse_noop),
    ]
