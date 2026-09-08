from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0031_rosa_nuevo_novedades'),
    ]

    operations = [
        migrations.AddField(
            model_name='datosempresa',
            name='porcentaje_riesgo_laboral',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('1.10'),
                max_digits=4,
                validators=[
                    MinValueValidator(Decimal('1.10')),
                    MaxValueValidator(Decimal('1.40')),
                ],
                verbose_name='Riesgo Laboral SRL (%)',
            ),
        ),
    ]
