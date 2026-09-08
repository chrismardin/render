import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0023_rrhh_modulos_internos'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='datosempresa',
            name='isr_ano_anterior',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.CreateModel(
            name='ConsultaIA',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('pregunta', models.TextField()),
                ('respuesta', models.TextField()),
                ('fecha', models.DateTimeField(auto_now_add=True)),
                ('usuario', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='consultas_ia', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-fecha']},
        ),
        migrations.AlterField(
            model_name='reportegenerado',
            name='tipo',
            field=models.CharField(
                choices=[
                    ('Financiero', 'Financiero'),
                    ('Ventas', 'Ventas'),
                    ('Inventario', 'Inventario'),
                    ('RRHH', 'RRHH'),
                    ('Impuestos', 'Impuestos'),
                ],
                max_length=20,
            ),
        ),
        migrations.AlterModelOptions(
            name='modulosistema',
            options={
                'managed': False,
                'default_permissions': (),
                'permissions': [
                    ('ver_dashboard', 'Puede ver Dashboard'),
                    ('ver_ventas', 'Puede ver Ventas'),
                    ('ver_productos', 'Puede ver Productos'),
                    ('crear_productos', 'Puede crear/editar Productos'),
                    ('ver_finanzas', 'Puede ver Finanzas'),
                    ('ver_inventario', 'Puede ver Inventario'),
                    ('ver_proyecciones', 'Puede ver Proyecciones'),
                    ('ver_clientes', 'Puede ver Clientes'),
                    ('ver_empleados', 'Puede ver RRHH'),
                    ('ver_impuestos', 'Puede ver Impuestos'),
                    ('ver_reportes', 'Puede ver Reportes'),
                    ('ver_modo_ia', 'Puede ver Modo IA'),
                    ('ver_configuracion', 'Puede ver Configuración'),
                    ('ver_usuarios', 'Puede ver y gestionar Usuarios'),
                    ('gestionar_roles', 'Puede crear, editar y eliminar Roles y Permisos'),
                    ('gestionar_recuperaciones', 'Puede gestionar solicitudes de recuperación de contraseña'),
                    ('ver_compras', 'Puede ver el módulo Compras'),
                    ('gestionar_proveedores', 'Puede crear y editar Proveedores'),
                    ('gestionar_ordenes_compra', 'Puede crear y gestionar Órdenes de compra'),
                    ('gestionar_recepciones', 'Puede registrar Recepciones'),
                    ('gestionar_compras', 'Puede registrar Compras y sus pagos'),
                    ('ver_pedidos_clientes', 'Puede ver Pedidos de Clientes'),
                    ('gestionar_pedidos_clientes', 'Puede crear y gestionar Pedidos de Clientes'),
                ],
            },
        ),
    ]
