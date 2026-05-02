from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0011_add_doc_cargo_type'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='DocumentType',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=300, verbose_name='Название документа')),
                ('description', models.TextField(blank=True, verbose_name='Описание')),
                ('category', models.CharField(
                    choices=[
                        ('transport',   'Транспортные документы'),
                        ('commercial',  'Коммерческие документы'),
                        ('customs',     'Таможенные документы'),
                        ('permit',      'Разрешительные документы'),
                        ('sanitary',    'Санитарные и ветеринарные'),
                        ('certificate', 'Сертификаты и декларации'),
                        ('other',       'Прочие'),
                    ],
                    default='other',
                    max_length=20,
                    verbose_name='Категория',
                )),
                ('is_active', models.BooleanField(default=True, verbose_name='Активен')),
            ],
            options={
                'verbose_name': 'Тип документа',
                'verbose_name_plural': 'Типы документов',
                'ordering': ['category', 'name'],
            },
        ),
        migrations.CreateModel(
            name='CargoCategoryDocRule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('cargo_type', models.CharField(
                    choices=[
                        ('B2C', 'B2C — Бизнес для потребителя'),
                        ('B2B', 'B2B — Бизнес для бизнеса'),
                        ('C2C', 'C2C — Частное лицо'),
                        ('DOC', 'Документация'),
                    ],
                    max_length=5,
                    verbose_name='Категория груза',
                )),
                ('document_type', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='category_rules',
                    to='cargo.documenttype',
                    verbose_name='Тип документа',
                )),
            ],
            options={
                'verbose_name': 'Правило документов по категории',
                'verbose_name_plural': 'Правила документов по категориям',
                'ordering': ['cargo_type', 'document_type__name'],
                'unique_together': {('cargo_type', 'document_type')},
            },
        ),
        migrations.CreateModel(
            name='HAWBChecklistItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('is_required', models.BooleanField(
                    default=True,
                    help_text='Обязателен для подачи ДТ по данной накладной',
                    verbose_name='Обязательный',
                )),
                ('is_received', models.BooleanField(default=False, verbose_name='Получен')),
                ('notes', models.TextField(blank=True, verbose_name='Примечания')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Добавлен')),
                ('added_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='added_checklist_items',
                    to=settings.AUTH_USER_MODEL,
                    verbose_name='Добавил',
                )),
                ('document_type', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to='cargo.documenttype',
                    verbose_name='Тип документа',
                )),
                ('hawb', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='checklist_items',
                    to='cargo.housewaybill',
                    verbose_name='Накладная',
                )),
            ],
            options={
                'verbose_name': 'Элемент чеклиста',
                'verbose_name_plural': 'Элементы чеклиста',
                'ordering': ['-is_required', 'document_type__name'],
                'unique_together': {('hawb', 'document_type')},
            },
        ),
    ]
