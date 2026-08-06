# Generated for release-reestr-mailer: terminal EXCLUDED status.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0072_cargo_transit_doc'),
    ]

    operations = [
        migrations.AlterField(
            model_name='releasereestrnotification',
            name='status',
            field=models.CharField(
                choices=[
                    ('SENT', 'Отправлено'),
                    ('FAILED', 'Ошибка отправки'),
                    ('SKIPPED', 'Отложено (нет подачи)'),
                    ('EXCLUDED', 'Исключено (не требуется)'),
                ],
                db_index=True, default='SENT', max_length=16, verbose_name='Статус'),
        ),
    ]
