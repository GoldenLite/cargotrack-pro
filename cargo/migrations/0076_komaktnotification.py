from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0075_outbox_parsed_meta_indexes'),
    ]

    operations = [
        migrations.CreateModel(
            name='KomaktNotification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('hawb_number', models.CharField(db_index=True, max_length=64, unique=True, verbose_name='Накладная')),
                ('to_email', models.CharField(blank=True, max_length=255, verbose_name='Кому')),
                ('status', models.CharField(choices=[('SENT', 'Отправлено'), ('FAILED', 'Ошибка'), ('SKIPPED', 'Отложено'), ('EXCLUDED', 'Исключено')], db_index=True, default='SENT', max_length=16, verbose_name='Статус')),
                ('registration_number', models.CharField(blank=True, max_length=64, verbose_name='Рег. номер ДТ')),
                ('num_positions', models.PositiveIntegerField(default=0, verbose_name='Позиций ДТ')),
                ('num_codes', models.PositiveIntegerField(default=0, verbose_name='Кодов ТН ВЭД')),
                ('release_date', models.DateTimeField(blank=True, null=True, verbose_name='Дата выпуска')),
                ('attempts', models.PositiveIntegerField(default=0, verbose_name='Попыток')),
                ('error', models.TextField(blank=True, verbose_name='Последняя ошибка')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('sent_at', models.DateTimeField(blank=True, null=True, verbose_name='Отправлено в')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
            ],
            options={
                'verbose_name': 'Рассылка комакта СВХ',
                'verbose_name_plural': 'Рассылки комактов СВХ',
                'ordering': ['-created_at'],
            },
        ),
    ]
