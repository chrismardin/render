import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0022_ventas_credito_y_tarjeta_finanzas'),
    ]

    operations = [
        migrations.CreateModel(
            name='DiaFeriado',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('fecha', models.DateField(unique=True)),
                ('nombre', models.CharField(max_length=100)),
            ],
            options={'ordering': ['fecha']},
        ),
        migrations.AddField(
            model_name='empleado',
            name='dias_vacaciones_tomados',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='empleado',
            name='horas_extra_mes',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=6),
        ),
        migrations.AddField(
            model_name='empleado',
            name='nivel_academico',
            field=models.CharField(blank=True, choices=[
                ('Primario', 'Primario'),
                ('Secundario', 'Secundario'),
                ('Tecnico', 'Tecnico'),
                ('Universitario', 'Universitario'),
                ('Postgrado', 'Postgrado'),
            ], max_length=20),
        ),
        migrations.AddField(
            model_name='empleado',
            name='tipo_empleado',
            field=models.CharField(choices=[('Fijo', 'Fijo'), ('Por Hora', 'Por Hora')], default='Fijo', max_length=15),
        ),
        migrations.CreateModel(
            name='Ausencia',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('fecha', models.DateField()),
                ('justificada', models.BooleanField(default=False)),
                ('motivo', models.CharField(blank=True, max_length=200)),
                ('empleado', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ausencias', to='core.empleado')),
            ],
            options={'ordering': ['-fecha']},
        ),
        migrations.AlterField(
            model_name='reportegenerado',
            name='tipo',
            field=models.CharField(choices=[
                ('Financiero', 'Financiero'),
                ('Ventas', 'Ventas'),
                ('Inventario', 'Inventario'),
                ('RRHH', 'RRHH'),
            ], max_length=20),
        ),
    ]
