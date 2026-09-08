from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


TRAMOS_ISR_2026 = [
    (1, Decimal('0'), Decimal('416220'), Decimal('0'), Decimal('0')),
    (2, Decimal('416220'), Decimal('624329'), Decimal('15'), Decimal('0')),
    (3, Decimal('624329'), Decimal('867123'), Decimal('20'), Decimal('31216')),
    (4, Decimal('867123'), None, Decimal('25'), Decimal('79776')),
]


def migrar_y_sembrar(apps, schema_editor):
    Producto = apps.get_model('core', 'Producto')
    Cliente = apps.get_model('core', 'Cliente')
    Venta = apps.get_model('core', 'Venta')
    DetalleVenta = apps.get_model('core', 'DetalleVenta')
    MovimientoFinanciero = apps.get_model('core', 'MovimientoFinanciero')
    CuentaPorCobrar = apps.get_model('core', 'CuentaPorCobrar')
    Cobro = apps.get_model('core', 'Cobro')
    CuentaBancaria = apps.get_model('core', 'CuentaBancaria')
    DatosEmpresa = apps.get_model('core', 'DatosEmpresa')
    TramoISR = apps.get_model('core', 'TramoISR')
    TopeTSS = apps.get_model('core', 'TopeTSS')

    # Congelar costo e impuesto histórico para ventas anteriores.
    for detalle in DetalleVenta.objects.select_related('venta', 'producto').all().iterator():
        cambios = []
        if detalle.costo_unitario == 0:
            detalle.costo_unitario = detalle.producto.costo_compra
            cambios.append('costo_unitario')
        if detalle.tasa_itbs == 0 and detalle.venta.subtotal:
            tasa = (detalle.venta.itbs / detalle.venta.subtotal * Decimal('100')).quantize(Decimal('0.01'))
            detalle.tasa_itbs = tasa
            cambios.append('tasa_itbs')
            neto = (detalle.cantidad * detalle.precio_unitario) - (
                detalle.cantidad * detalle.precio_unitario * detalle.descuento_porcentaje / Decimal('100')
            )
            detalle.itbs_monto = (neto * tasa / Decimal('100')).quantize(Decimal('0.01'))
            cambios.append('itbs_monto')
        if cambios:
            detalle.save(update_fields=cambios)

    # Cuentas base aportadas por Rosa.
    caja, _ = CuentaBancaria.objects.get_or_create(nombre='Caja General', defaults={'tipo': 'Caja'})
    tarjeta, _ = CuentaBancaria.objects.get_or_create(nombre='Tarjetas por liquidar', defaults={'tipo': 'Tarjeta'})
    banco, _ = CuentaBancaria.objects.get_or_create(nombre='Banco Principal', defaults={'tipo': 'Banco'})

    for mov in MovimientoFinanciero.objects.filter(cuenta__isnull=True).iterator():
        medio = (mov.medio_pago or '').lower()
        if 'tarjeta' in medio:
            mov.cuenta = tarjeta
        elif 'efectivo' in medio or 'caja' in medio:
            mov.cuenta = caja
        else:
            mov.cuenta = banco
        mov.save(update_fields=['cuenta'])

    # En la rama Chris antigua "Credito" era método; Mónica separa condición y método.
    try:
        dias_credito = DatosEmpresa.objects.get(pk=1).dias_credito or 30
    except DatosEmpresa.DoesNotExist:
        dias_credito = 30

    for venta in Venta.objects.filter(metodo_pago='Credito').order_by('pk'):
        venta.condicion_pago = 'Credito'
        # El esquema histórico no guardaba el medio real de cobro futuro.
        # Se deja Efectivo como valor técnico compatible y la condición conserva el crédito.
        venta.metodo_pago = 'Efectivo'
        venta.save(update_fields=['condicion_pago', 'metodo_pago'])
        if not venta.cliente_id:
            continue
        fecha_emision = venta.fecha.date()
        cuenta, _ = CuentaPorCobrar.objects.get_or_create(
            venta_id=venta.pk,
            defaults={
                'cliente_id': venta.cliente_id,
                'importe_original': venta.total,
                'fecha_emision': fecha_emision,
                'fecha_vencimiento': fecha_emision + timedelta(days=dias_credito),
                'creado_por_id': venta.vendedor_id,
            },
        )
        # Reconstruye abonos históricos que aún no estuvieran enlazados a Cobro.
        for mov in MovimientoFinanciero.objects.filter(
            venta_id=venta.pk, tipo='Ingreso', cobro__isnull=True
        ).order_by('fecha', 'pk'):
            cobro = Cobro.objects.create(
                cuenta_id=cuenta.pk,
                monto=mov.monto,
                fecha=mov.fecha,
                metodo_pago=mov.medio_pago or '',
                referencia=mov.factura or '',
                observacion='Migrado desde movimiento financiero existente',
            )
            mov.cobro_id = cobro.pk
            mov.save(update_fields=['cobro'])

    # Escala ISR y topes TSS de Mónica actual.
    for orden, inferior, superior, tasa, acumulado in TRAMOS_ISR_2026:
        TramoISR.objects.update_or_create(
            anio=2026,
            orden=orden,
            defaults={
                'limite_inferior': inferior,
                'limite_superior': superior,
                'tasa': tasa,
                'monto_acumulado': acumulado,
            },
        )
    TopeTSS.objects.update_or_create(
        anio=2026,
        defaults={
            'tope_afp': Decimal('464460.00'),
            'tope_sfs': Decimal('232230.00'),
        },
    )


def crear_permisos(apps, schema_editor):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    Group = apps.get_model('auth', 'Group')

    ct, _ = ContentType.objects.get_or_create(app_label='core', model='modulosistema')
    defs = {
        'ver_cuentas_por_cobrar': 'Puede ver Cuentas por Cobrar',
        'registrar_cobros': 'Puede registrar cobros de Cuentas por Cobrar',
        'registrar_pagos_compra': 'Puede registrar pagos de Cuentas por Pagar',
        'gestionar_empleados': 'Puede crear y editar Empleados',
        'ver_nomina': 'Puede ver el módulo Nómina',
        'gestionar_nomina': 'Puede generar y procesar Nómina',
        'ver_ausencias': 'Puede ver Ausencias',
        'gestionar_ausencias': 'Puede registrar y editar Ausencias',
        'ver_feriados': 'Puede ver Días Feriados',
        'gestionar_feriados': 'Puede crear y editar Días Feriados',
        'registrar_pago_nomina': 'Puede registrar el pago de una Nómina',
    }
    permisos = {}
    for codename, name in defs.items():
        p, _ = Permission.objects.get_or_create(
            content_type=ct, codename=codename, defaults={'name': name}
        )
        if p.name != name:
            p.name = name
            p.save(update_fields=['name'])
        permisos[codename] = p

    por_grupo = {
        'Administrador': list(defs),
        'Gerente': [
            'ver_cuentas_por_cobrar', 'registrar_cobros', 'registrar_pagos_compra',
            'ver_nomina', 'ver_ausencias', 'ver_feriados',
        ],
        'Finanzas': [
            'ver_cuentas_por_cobrar', 'registrar_cobros', 'registrar_pagos_compra',
            'ver_nomina', 'registrar_pago_nomina',
        ],
        'Compras': ['registrar_pagos_compra'],
    }
    for nombre, codigos in por_grupo.items():
        grupo = Group.objects.filter(name=nombre).first()
        if grupo:
            grupo.permissions.add(*(permisos[c] for c in codigos))


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0028_merge_chris_monica'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CuentaBancaria',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('nombre', models.CharField(max_length=100)),
                ('tipo', models.CharField(choices=[('Banco', 'Cuenta Bancaria'), ('Caja', 'Caja / Efectivo'), ('Tarjeta', 'Tarjetas por liquidar')], default='Banco', max_length=10)),
                ('banco', models.CharField(blank=True, max_length=100)),
                ('numero_cuenta', models.CharField(blank=True, max_length=40)),
                ('activa', models.BooleanField(default=True)),
            ],
            options={'ordering': ['nombre']},
        ),
        migrations.AddField(
            model_name='producto', name='exento_itbis',
            field=models.BooleanField(default=False, verbose_name='Exento de ITBIS'),
        ),
        migrations.AddField(
            model_name='cliente', name='condicion_fiscal',
            field=models.CharField(choices=[('Normal', 'Normal'), ('Exento', 'Exento / Régimen especial')], default='Normal', max_length=10),
        ),
        migrations.AddField(
            model_name='cliente', name='activo', field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='venta', name='tipo_comprobante',
            field=models.CharField(choices=[('E32', 'E32 - Consumo'), ('E31', 'E31 - Crédito Fiscal'), ('E44', 'E44 - Regímenes Especiales')], default='E32', max_length=3),
        ),
        migrations.AddField(
            model_name='detalleventa', name='costo_unitario',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=10),
        ),
        migrations.AddField(
            model_name='detalleventa', name='tasa_itbs',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=5),
        ),
        migrations.AddField(
            model_name='detalleventa', name='itbs_monto',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='empleado', name='area',
            field=models.CharField(blank=True, max_length=100, verbose_name='Área / Departamento interno'),
        ),
        migrations.AlterField(
            model_name='ausencia', name='empleado',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ausencias', to='core.empleado'),
        ),
        migrations.AlterModelOptions(
            name='ausencia',
            options={'ordering': ['-fecha'], 'verbose_name': 'Ausencia', 'verbose_name_plural': 'Ausencias'},
        ),
        migrations.AlterModelOptions(
            name='diaferiado',
            options={'ordering': ['fecha'], 'verbose_name': 'Día feriado', 'verbose_name_plural': 'Días feriados'},
        ),
        migrations.AddField(
            model_name='datosempresa', name='afp_porcentaje',
            field=models.DecimalField(decimal_places=2, default=Decimal('2.87'), max_digits=5, verbose_name='AFP (%)'),
        ),
        migrations.AddField(
            model_name='datosempresa', name='sfs_porcentaje',
            field=models.DecimalField(decimal_places=2, default=Decimal('3.04'), max_digits=5, verbose_name='SFS (%)'),
        ),
        migrations.AddField(
            model_name='datosempresa', name='dias_mes_calculo',
            field=models.DecimalField(decimal_places=2, default=Decimal('23.83'), max_digits=5, verbose_name='Días de referencia para cálculo diario/hora extra'),
        ),
        migrations.AddField(
            model_name='datosempresa', name='factor_hora_extra',
            field=models.DecimalField(decimal_places=2, default=Decimal('1.35'), max_digits=4, verbose_name='Factor de pago de hora extra'),
        ),
        migrations.CreateModel(
            name='TramoISR',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('anio', models.PositiveIntegerField(default=2026, verbose_name='Año fiscal')),
                ('orden', models.PositiveSmallIntegerField()),
                ('limite_inferior', models.DecimalField(decimal_places=2, max_digits=12)),
                ('limite_superior', models.DecimalField(blank=True, decimal_places=2, help_text='Vacío = sin tope (último tramo).', max_digits=12, null=True)),
                ('tasa', models.DecimalField(decimal_places=2, max_digits=5, verbose_name='Tasa (%)')),
                ('monto_acumulado', models.DecimalField(decimal_places=2, default=0, max_digits=12, verbose_name='ISR acumulado de tramos anteriores')),
            ],
            options={'ordering': ['anio', 'orden'], 'verbose_name': 'Tramo de ISR', 'verbose_name_plural': 'Escala de ISR (tramos)'},
        ),
        migrations.AddConstraint(
            model_name='tramoisr',
            constraint=models.UniqueConstraint(fields=('anio', 'orden'), name='unico_tramo_isr_por_anio'),
        ),
        migrations.CreateModel(
            name='TopeTSS',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('anio', models.PositiveIntegerField(unique=True, verbose_name='Año fiscal')),
                ('tope_afp', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, verbose_name='Tope de cotización AFP (mensual)')),
                ('tope_sfs', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, verbose_name='Tope de cotización SFS (mensual)')),
            ],
            options={'ordering': ['-anio'], 'verbose_name': 'Tope TSS', 'verbose_name_plural': 'Topes TSS (AFP/SFS) por año'},
        ),
        migrations.CreateModel(
            name='Nomina',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('fecha_inicio', models.DateField()),
                ('fecha_fin', models.DateField()),
                ('fecha_generacion', models.DateTimeField(auto_now_add=True)),
                ('estado', models.CharField(choices=[('Pendiente de pago', 'Pendiente de pago'), ('Pagada', 'Pagada')], default='Pendiente de pago', max_length=20)),
                ('total_bruto', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('total_deducciones', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('total_neto', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('fecha_pago', models.DateField(blank=True, null=True)),
                ('generada_por', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='nominas_generadas', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-fecha_inicio'], 'verbose_name': 'Nómina', 'verbose_name_plural': 'Nóminas'},
        ),
        migrations.AddConstraint(
            model_name='nomina',
            constraint=models.UniqueConstraint(fields=('fecha_inicio',), name='unico_periodo_de_nomina'),
        ),
        migrations.CreateModel(
            name='DetalleNomina',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('empleado_nombre', models.CharField(max_length=120)),
                ('salario_base', models.DecimalField(decimal_places=2, max_digits=12)),
                ('dias_ausencia', models.PositiveIntegerField(default=0)),
                ('horas_extra', models.DecimalField(decimal_places=2, default=0, max_digits=6)),
                ('pago_horas_extra', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('afp', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('sfs', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('isr', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('descuento_ausencias', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('salario_bruto', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('total_deducciones', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('salario_neto', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('empleado', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='detalles_nomina', to='core.empleado')),
                ('nomina', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='detalles', to='core.nomina')),
            ],
            options={'ordering': ['empleado_nombre'], 'verbose_name': 'Detalle de nómina', 'verbose_name_plural': 'Detalles de nómina'},
        ),
        migrations.AddConstraint(
            model_name='detallenomina',
            constraint=models.UniqueConstraint(fields=('nomina', 'empleado'), name='unico_empleado_por_nomina'),
        ),
        migrations.AddField(
            model_name='movimientofinanciero', name='cuenta',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='movimientos', to='core.cuentabancaria'),
        ),
        migrations.AddField(
            model_name='movimientofinanciero', name='nomina',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='movimiento_financiero', to='core.nomina'),
        ),
        migrations.AlterField(
            model_name='venta', name='metodo_pago',
            field=models.CharField(choices=[('Efectivo', 'Efectivo'), ('Tarjeta', 'Tarjeta')], default='Efectivo', max_length=10),
        ),
        migrations.AlterField(
            model_name='venta', name='condicion_pago',
            field=models.CharField(choices=[('Contado', 'Contado'), ('Credito', 'Crédito')], default='Contado', max_length=10),
        ),
        migrations.AlterField(
            model_name='cobro', name='metodo_pago',
            field=models.CharField(blank=True, choices=[('Banco BHD', 'Banco BHD'), ('Caja Chica', 'Caja Chica'), ('Transferencia', 'Transferencia'), ('Efectivo', 'Efectivo'), ('Tarjeta', 'Tarjeta')], max_length=20),
        ),
        migrations.AlterField(
            model_name='pagocompra', name='metodo_pago',
            field=models.CharField(blank=True, choices=[('Banco BHD', 'Banco BHD'), ('Caja Chica', 'Caja Chica'), ('Transferencia', 'Transferencia'), ('Efectivo', 'Efectivo'), ('Tarjeta', 'Tarjeta')], max_length=20),
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
                    ('gestionar_compras', 'Puede registrar Compras'),
                    ('ver_pedidos_clientes', 'Puede ver Pedidos de Clientes'),
                    ('gestionar_pedidos_clientes', 'Puede crear y gestionar Pedidos de Clientes'),
                    ('ver_cuentas_por_cobrar', 'Puede ver Cuentas por Cobrar'),
                    ('registrar_cobros', 'Puede registrar cobros de Cuentas por Cobrar'),
                    ('registrar_pagos_compra', 'Puede registrar pagos de Cuentas por Pagar'),
                    ('gestionar_empleados', 'Puede crear y editar Empleados'),
                    ('ver_nomina', 'Puede ver el módulo Nómina'),
                    ('gestionar_nomina', 'Puede generar y procesar Nómina'),
                    ('ver_ausencias', 'Puede ver Ausencias'),
                    ('gestionar_ausencias', 'Puede registrar y editar Ausencias'),
                    ('ver_feriados', 'Puede ver Días Feriados'),
                    ('gestionar_feriados', 'Puede crear y editar Días Feriados'),
                    ('registrar_pago_nomina', 'Puede registrar el pago de una Nómina'),
                ],
            },
        ),
    ]
