# Phase 1 Deklarant Plus integration:
# - DeklarantSession model (QR-сессия к API «Декларант Плюс»)
# - Cargo.svh_source (маркер источника СВХ-данных для арбитража провайдеров)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0066_crmhawbindex_dup_unique'),
    ]

    operations = [
        migrations.CreateModel(
            name='DeklarantSession',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('login', models.CharField(blank=True, max_length=128, verbose_name='Login')),
                ('session_id', models.CharField(max_length=128, verbose_name='Session GUID')),
                ('is_mobile', models.BooleanField(default=True, verbose_name='IsMobileUser')),
                ('is_active', models.BooleanField(db_index=True, default=True, verbose_name='Активна')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создана')),
                ('last_used_at', models.DateTimeField(blank=True, null=True, verbose_name='Последнее использование')),
                ('last_error', models.TextField(blank=True, verbose_name='Последняя ошибка')),
            ],
            options={
                'verbose_name': 'Сессия Декларант Плюс',
                'verbose_name_plural': 'Сессии Декларант Плюс',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddField(
            model_name='cargo',
            name='svh_source',
            field=models.CharField(
                blank=True, default='', db_index=True, max_length=20,
                choices=[
                    ('', '—'),
                    ('alta', 'Альта-СВХ (Внуково)'),
                    ('moscow_cargo', 'Москва-Карго (Шереметьево)'),
                    ('deklarant', 'Декларант Плюс (Таможенный портал ДВ)'),
                    ('manual', 'Вручную'),
                ],
                verbose_name='Источник СВХ-данных',
            ),
        ),
    ]
