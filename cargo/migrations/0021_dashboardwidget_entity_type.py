from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0020_workflow_entity_binding'),
    ]

    operations = [
        migrations.AddField(
            model_name='dashboardwidget',
            name='entity_type',
            field=models.CharField(
                choices=[('cargo', 'Партии (MAWB)'), ('hawb', 'Накладные (HAWB)')],
                default='cargo',
                max_length=10,
                verbose_name='Сущность',
            ),
        ),
    ]
