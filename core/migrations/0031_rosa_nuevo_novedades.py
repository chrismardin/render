from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0030_datos_integracion_monica_rosa'),
    ]

    operations = [
        migrations.AddField(
            model_name='empleado',
            name='tipo_nomina',
            field=models.CharField(
                choices=[('Mensual', 'Mensual'), ('Quincenal', 'Quincenal')],
                default='Mensual',
                max_length=10,
                verbose_name='Tipo de nómina',
            ),
        ),
        migrations.AddField(
            model_name='detallenomina',
            name='tipo_nomina',
            field=models.CharField(default='Mensual', max_length=10),
        ),
        migrations.AddField(
            model_name='detallenomina',
            name='primera_quincena',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='detallenomina',
            name='segunda_quincena',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
    ]
