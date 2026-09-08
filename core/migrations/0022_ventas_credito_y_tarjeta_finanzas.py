from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0021_conectar_pedido_venta_y_trazabilidad_finanzas'),
    ]

    operations = [
        migrations.AlterField(
            model_name='movimientofinanciero',
            name='medio_pago',
            field=models.CharField(
                blank=True,
                choices=[
                    ('Banco BHD', 'Banco BHD'),
                    ('Caja Chica', 'Caja Chica'),
                    ('Transferencia', 'Transferencia'),
                    ('Efectivo', 'Efectivo'),
                    ('Tarjeta', 'Tarjeta'),
                ],
                max_length=20,
            ),
        ),
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
