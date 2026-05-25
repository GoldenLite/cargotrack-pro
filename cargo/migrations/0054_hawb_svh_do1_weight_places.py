from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cargo', '0053_cargo_svh_do1_sent_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='housewaybill',
            name='svh_do1_gross_weight',
            field=models.DecimalField(
                blank=True, decimal_places=3, max_digits=12, null=True,
                help_text=(
                    'BruttoVolQuant.GoodsQuantity из <Goods> блока с этим HAWB '
                    'в исходящем ДО-1 (do1-*.xml в backup_out). Кг.'
                ),
                verbose_name='Вес ДО1',
            ),
        ),
        migrations.AddField(
            model_name='housewaybill',
            name='svh_do1_place_count',
            field=models.IntegerField(
                blank=True, null=True,
                help_text='CargoPlace.PlaceNumber из <Goods> блока с этим HAWB в ДО-1.',
                verbose_name='Мест ДО1',
            ),
        ),
    ]
