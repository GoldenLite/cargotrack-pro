from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0062_housewaybill_declarant_name'),
    ]

    operations = [
        migrations.CreateModel(
            name='CrmHawbIndex',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('hawb_number', models.CharField(db_index=True, max_length=64, verbose_name='HAWB')),
                ('tab_name', models.CharField(max_length=64, verbose_name='Вкладка')),
                ('row_index', models.PositiveIntegerField(db_index=True, verbose_name='№ строки')),
                ('last_decl', models.CharField(blank=True, default='', max_length=64, verbose_name='Last decl')),
                ('last_status', models.CharField(blank=True, default='', max_length=128, verbose_name='Last ed_status')),
                ('last_request', models.TextField(blank=True, default='', verbose_name='Last request')),
                ('last_arrival', models.CharField(blank=True, default='', max_length=16, verbose_name='Last arrival')),
                ('last_warehouse', models.CharField(blank=True, default='', max_length=32, verbose_name='Last warehouse')),
                ('last_hidden', models.BooleanField(default=False, verbose_name='Скрыта')),
                ('last_seen_at', models.DateTimeField(auto_now_add=True, verbose_name='В Sheets увидели')),
                ('last_synced_at', models.DateTimeField(blank=True, null=True, verbose_name='Последний sync')),
            ],
            options={
                'verbose_name': 'CRM-индекс HAWB',
                'verbose_name_plural': 'CRM-индекс HAWB',
                'unique_together': {('hawb_number', 'tab_name')},
                'indexes': [
                    models.Index(fields=['tab_name', 'row_index'], name='cargo_crmha_tab_nam_idx'),
                    models.Index(fields=['hawb_number'], name='cargo_crmha_hawb_nu_idx'),
                ],
            },
        ),
    ]
