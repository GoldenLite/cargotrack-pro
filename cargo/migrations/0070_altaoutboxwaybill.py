from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    # Аддитивно: только создаём новую таблицу. parsed_meta не трогаем —
    # откат тривиален (reverse = DeleteModel), исходные данные целы.

    dependencies = [
        ('cargo', '0069_alter_cargo_svh_source'),
    ]

    operations = [
        migrations.CreateModel(
            name='AltaOutboxWaybill',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('hawb_number', models.CharField(db_index=True, max_length=64,
                                                 verbose_name='Номер накладной')),
                ('observation', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='waybill_refs',
                    to='cargo.altaoutboxobservation',
                    verbose_name='Исходящая копия')),
            ],
            options={
                'verbose_name': 'Накладная в исходящей копии',
                'verbose_name_plural': 'Накладные в исходящих копиях',
            },
        ),
        migrations.AddConstraint(
            model_name='altaoutboxwaybill',
            constraint=models.UniqueConstraint(
                fields=('observation', 'hawb_number'),
                name='uniq_outbox_waybill'),
        ),
    ]
