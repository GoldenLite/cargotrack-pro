from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0014_cargo_type_doc_template'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='hawbgood',
            name='approval_status',
            field=models.CharField(
                choices=[
                    ('pending', 'На согласовании'),
                    ('approved', 'Согласовано'),
                    ('clarification', 'Требует уточнения'),
                    ('rejected', 'Отклонено'),
                ],
                db_index=True,
                default='pending',
                max_length=20,
                verbose_name='Статус согласования',
            ),
        ),
        migrations.AddField(
            model_name='hawbgood',
            name='approval_comment',
            field=models.TextField(blank=True, verbose_name='Комментарий к согласованию'),
        ),
        migrations.AddField(
            model_name='hawbgood',
            name='approved_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='approved_goods',
                to=settings.AUTH_USER_MODEL,
                verbose_name='Согласовал',
            ),
        ),
        migrations.AddField(
            model_name='hawbgood',
            name='approved_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Дата согласования'),
        ),
    ]
