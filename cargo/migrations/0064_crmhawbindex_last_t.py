from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0063_crmhawbindex'),
    ]

    operations = [
        migrations.AddField(
            model_name='crmhawbindex',
            name='last_t',
            field=models.BooleanField(
                default=False,
                verbose_name='T checkbox (подано/в работе/выпущено)'),
        ),
    ]
