from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0021_dashboardwidget_entity_type'),
    ]

    operations = [
        migrations.AlterField(
            model_name='dashboardwidget',
            name='widget_type',
            field=models.CharField(
                choices=[
                    ('stat',              'Метрика'),
                    ('kanban',            'Канбан'),
                    ('table',             'Таблица'),
                    ('chart_stage',       'График: этапы'),
                    ('chart_warehouse',   'График: склады'),
                    ('forecast_arrivals', 'Прогноз прилётов'),
                    ('pivot',             'Сводная таблица'),
                ],
                default='stat',
                max_length=30,
                verbose_name='Тип',
            ),
        ),
    ]
