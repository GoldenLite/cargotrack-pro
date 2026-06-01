from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0065_cdek_tracking'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='crmhawbindex',
            unique_together={('hawb_number', 'tab_name', 'row_index')},
        ),
    ]
