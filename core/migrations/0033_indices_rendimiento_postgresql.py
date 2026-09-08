from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0032_datosempresa_porcentaje_riesgo_laboral')]

    operations = [
        migrations.AddIndex(
            model_name='movimientostock',
            index=models.Index(fields=['tipo', '-fecha'], name='movstock_tipo_fecha_idx'),
        ),
        migrations.AddIndex(
            model_name='movimientofinanciero',
            index=models.Index(fields=['tipo', '-fecha'], name='movfin_tipo_fecha_idx'),
        ),
        migrations.AddIndex(
            model_name='venta',
            index=models.Index(fields=['-fecha'], name='venta_fecha_idx'),
        ),
        migrations.AddIndex(
            model_name='empleado',
            index=models.Index(fields=['estado', 'nombre'], name='empleado_estado_nombre_idx'),
        ),
        migrations.AddIndex(
            model_name='ausencia',
            index=models.Index(fields=['fecha', 'justificada', 'empleado'], name='ausencia_fecha_estado_idx'),
        ),
    ]
