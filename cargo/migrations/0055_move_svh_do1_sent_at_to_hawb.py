from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0054_hawb_svh_do1_weight_places'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='cargo',
            name='svh_do1_sent_at',
        ),
        migrations.AddField(
            model_name='housewaybill',
            name='svh_do1_sent_at',
            field=models.DateTimeField(
                blank=True, null=True,
                help_text=(
                    'Момент когда Альта-СВХ отправила ДО-1 в таможню (mtime '
                    'файла do1-*.xml в backup_out). Per-HAWB — только для тех '
                    'накладных что упомянуты в конкретном ДО-1 (одна партия '
                    'может иметь несколько ДО-1 с разными списками HAWB).'
                ),
                verbose_name='Дата подачи ДО1',
            ),
        ),
    ]
