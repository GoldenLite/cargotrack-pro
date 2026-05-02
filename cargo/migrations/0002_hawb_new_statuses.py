from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='housewaybill',
            name='logistics_status',
            field=models.CharField(
                verbose_name='Логистический статус',
                max_length=20,
                default='CREATED',
                db_index=True,
                choices=[
                    ('CREATED',         'Создан'),
                    ('TO_ORIGIN_WH',    'В пути на склад отправки'),
                    ('AT_ORIGIN_WH',    'Принят на склад отправки'),
                    ('CONSOLIDATED',    'Добавлен в консоль для отправки'),
                    ('READY_TO_SHIP',   'Готов к отправке'),
                    ('EXPORT_CUSTOMS',  'Экспортное таможенное оформление'),
                    ('IN_TRANSIT_EXP',  'Груз в пути в страну назначения'),
                    ('ARRIVED_DEST',    'Груз прибыл в страну назначения'),
                    ('AT_SVH',          'Груз размещён на складе временного хранения'),
                    ('IMPORT_CUSTOMS',  'Таможенное оформление в стране назначения'),
                    ('READY_DELIVERY',  'Груз готов к отгрузке со СВХ'),
                    ('TO_SORT_CENTER',  'Отгрузка со СВХ на сортировочный центр'),
                    ('AT_SORT_CENTER',  'Приёмка на сортировочном центре'),
                    ('READY_TO_DEST',   'Груз готов к отправке в пункт назначения'),
                    ('IN_TRANSIT_DEST', 'Груз в пути в пункт назначения'),
                    ('ARRIVED_FINAL',   'Груз прибыл в пункт назначения'),
                    ('DELIVERED',       'Вручено'),
                    ('RETURNED',        'Возврат отправителю'),
                    ('LOST',            'Утеря груза'),
                ],
            ),
        ),
        migrations.AddField(
            model_name='housewaybill',
            name='logistics_status_date',
            field=models.DateTimeField(
                verbose_name='Дата лог. статуса',
                null=True, blank=True,
            ),
        ),
        migrations.AddField(
            model_name='housewaybill',
            name='customs_status_date',
            field=models.DateTimeField(
                verbose_name='Дата там. статуса',
                null=True, blank=True,
            ),
        ),
        migrations.AddField(
            model_name='housewaybill',
            name='customs_status',
            field=models.CharField(
                verbose_name='Таможенный статус',
                max_length=20,
                blank=True, default='',
                choices=[
                    ('',               '—'),
                    ('BROKER_CHECK',   'Проверка брокером'),
                    ('READY_TO_FILE',  'Готов к подаче'),
                    ('FILED',          'Подан на таможенное оформление'),
                    ('EXAMINATION',    'Досмотр'),
                    ('HOLD',           'Удержан таможней'),
                    ('RELEASED',       'Выпущен'),
                    ('REJECTED',       'Отказ в выпуске'),
                ],
            ),
        ),
    ]
