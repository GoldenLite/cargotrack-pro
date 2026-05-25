from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0052_remove_cargo_svh_do2_reg_number_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='cargo',
            name='svh_do1_sent_at',
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    'Момент когда Альта-СВХ отправила ДО-1 в таможню. '
                    'Берётся из mtime файла do1-*.xml в backup_out (наблюдается '
                    'agent-ом). Отличается от scan_into_bond — там момент '
                    'регистрации от таможни, тут момент нашей подачи.'
                ),
                null=True,
                verbose_name='Дата подачи ДО1',
            ),
        ),
    ]
