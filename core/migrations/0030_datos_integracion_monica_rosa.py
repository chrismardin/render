from datetime import timedelta
from decimal import Decimal

from django.db import migrations


TRAMOS_ISR_2026 = [
    (1, Decimal('0'), Decimal('416220'), Decimal('0'), Decimal('0')),
    (2, Decimal('416220'), Decimal('624329'), Decimal('15'), Decimal('0')),
    (3, Decimal('624329'), Decimal('867123'), Decimal('20'), Decimal('31216')),
    (4, Decimal('867123'), None, Decimal('25'), Decimal('79776')),
]


def migrar_y_sembrar(apps, schema_editor):
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
        # El histórico no guardaba el medio de cobro futuro; la condición conserva el crédito.
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
        ('core', '0029_integracion_monica_actual_rosa'),
    ]

    operations = [
        migrations.RunPython(migrar_y_sembrar, migrations.RunPython.noop),
        migrations.RunPython(crear_permisos, migrations.RunPython.noop),
    ]
