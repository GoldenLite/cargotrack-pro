from django.db import migrations, models
import django.db.models.deletion


def create_templates_and_link(apps, schema_editor):
    CargoTypeDocTemplate = apps.get_model('cargo', 'CargoTypeDocTemplate')
    CargoCategoryDocRule = apps.get_model('cargo', 'CargoCategoryDocRule')

    cargo_types = ['B2C', 'B2B', 'C2C', 'DOC']
    templates = {}
    for ct in cargo_types:
        tpl, _ = CargoTypeDocTemplate.objects.get_or_create(cargo_type=ct)
        templates[ct] = tpl

    for rule in CargoCategoryDocRule.objects.all():
        if rule.cargo_type in templates:
            rule.template = templates[rule.cargo_type]
            rule.save(update_fields=['template'])


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0013_initial_document_types'),
    ]

    operations = [
        migrations.CreateModel(
            name='CargoTypeDocTemplate',
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
                    unique=True,
                    verbose_name='Категория груза',
                )),
            ],
            options={
                'verbose_name': 'Шаблон документов',
                'verbose_name_plural': 'Шаблоны документов по категориям',
                'ordering': ['cargo_type'],
            },
        ),
        migrations.AddField(
            model_name='cargocategorydocrule',
            name='template',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='rules',
                to='cargo.cargotypedoctemplate',
                verbose_name='Шаблон категории',
            ),
        ),
        migrations.RunPython(create_templates_and_link, migrations.RunPython.noop),
    ]
