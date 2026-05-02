from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0002_hawb_new_statuses'),
    ]

    operations = [
        migrations.AddField(
            model_name='cargo',
            name='stage',
            field=models.CharField(
                verbose_name='Этап', max_length=15, default='DRAFT', db_index=True,
                choices=[
                    ('DRAFT',      'Формирование партии'),
                    ('FORMED',     'Партия сформирована'),
                    ('DISPATCHED', 'Партия отправлена в страну назначения'),
                    ('ARRIVED',    'Партия прибыла в страну назначения'),
                    ('CUSTOMS',    'Таможенное оформление в стране назначения'),
                    ('RELEASED',   'Полный выпуск'),
                ],
            ),
        ),
        migrations.AddField(
            model_name='cargo',
            name='is_draft',
            field=models.BooleanField(
                verbose_name='Черновик', default=True,
                help_text='Черновик — партия ещё формируется',
            ),
        ),
        migrations.AddField(
            model_name='cargo',
            name='stage_changed_at',
            field=models.DateTimeField(
                verbose_name='Дата смены этапа', null=True, blank=True,
            ),
        ),
        migrations.AlterField(
            model_name='cargo',
            name='queue',
            field=models.CharField(
                verbose_name='Очередь (устар.)', max_length=15, default='DRAFT', db_index=True,
                choices=[
                    ('DRAFT',      'Формирование партии'),
                    ('FORMED',     'Партия сформирована'),
                    ('DISPATCHED', 'Партия отправлена в страну назначения'),
                    ('ARRIVED',    'Партия прибыла в страну назначения'),
                    ('CUSTOMS',    'Таможенное оформление в стране назначения'),
                    ('RELEASED',   'Полный выпуск'),
                ],
            ),
        ),
    ]
