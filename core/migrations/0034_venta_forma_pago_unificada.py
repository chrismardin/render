from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0033_indices_rendimiento_postgresql'),
    ]

    operations = [
        migrations.AlterField(
            model_name='venta',
            name='metodo_pago',
            field=models.CharField(
                choices=[
                    ('Efectivo', 'Efectivo'),
                    ('Tarjeta', 'Tarjeta'),
                    ('Credito', 'Crédito'),
                ],
                default='Efectivo',
                max_length=10,
            ),
        ),
    ]
