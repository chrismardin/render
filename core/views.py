import calendar
from io import BytesIO
from decimal import Decimal, InvalidOperation
from datetime import timedelta, datetime
from collections import defaultdict
from types import SimpleNamespace
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.db import models, transaction
from django.db.models.functions import Coalesce, TruncMonth
from django.urls import reverse
from django.utils import timezone
from django.utils.timezone import now
from django.core.files.base import ContentFile
from django.core.exceptions import PermissionDenied, ValidationError
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.contrib.auth.models import User
import json
from django.contrib.auth.forms import PasswordChangeForm, AdminPasswordChangeForm
from django.contrib.auth import update_session_auth_hash
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from django.contrib.auth.decorators import permission_required
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.forms import SetPasswordForm
from .permisos import es_administrador, es_ultimo_administrador, GRUPOS_DEL_SISTEMA
from .proteccion_administrador import permisos_criticos_faltantes
from .permisos_django import requiere_permiso, permiso_modulo_requerido, puede_escribir_en
from .roles_permisos import construir_matriz_permisos, permisos_validos_desde_ids
from .recuperacion import generar_autorizacion, autorizacion_valida, invalidar_autorizacion
from .models import (
    Producto, MovimientoStock, MovimientoFinanciero, Cliente, Venta,
    DetalleVenta, Empleado, DiaFeriado, Ausencia, ReporteGenerado, DatosEmpresa,
    PreferenciasNotificacion, ConsultaIA, SolicitudRecuperacionPassword,
    Proveedor, OrdenCompra, DetalleOrdenCompra, Recepcion, DetalleRecepcion, Compra,
    PedidoCliente, DetallePedidoCliente, CuentaPorCobrar, Cobro, PagoCompra, CuentaBancaria,
    Nomina, DetalleNomina,
)
from . import servicios_nomina as nomina_servicio
from .forms import (
    MovimientoStockForm, MovimientoFinancieroForm, ProductoForm, ClienteForm,
    EmpleadoForm, FeriadoForm, DiaFeriadoForm, AusenciaForm, PerfilForm, DatosEmpresaForm, PreferenciasNotificacionForm,
    CrearUsuarioForm, EditarUsuarioForm, RolForm, SolicitarRecuperacionForm,
    ProveedorForm, OrdenCompraForm, DetalleOrdenCompraFormSet, CompraForm,
    PedidoClienteForm, DetallePedidoClienteFormSet, CobroForm, PagoCompraForm, CuentaBancariaForm,
)

def _nomina_mensual_actual():
    """Costo mensual programado de los empleados activos.

    Se calcula dentro de PostgreSQL para transferir una sola cifra en vez de
    descargar cada empleado en cada visita al dashboard/reportes.
    """
    return Empleado.objects.filter(estado='Activo').aggregate(
        total=models.Sum('salario')
    )['total'] or Decimal('0')


def _meses_en_periodo(desde, hasta):
    """Devuelve el primer día de cada mes tocado por un periodo."""
    cursor = desde.replace(day=1)
    limite = hasta.replace(day=1)
    meses = []
    while cursor <= limite:
        meses.append(cursor)
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)
    return meses

def _rango_datetime(desde, hasta):
    """Límites [inicio, fin) para filtrar DateTimeField sin ``__date``.

    Evitar ``fecha__date`` permite que PostgreSQL aproveche un índice normal
    sobre el DateTimeField y además evita aplicar una función SQL a cada fila.
    """
    inicio_naive = datetime.combine(desde, datetime.min.time())
    fin_naive = datetime.combine(hasta + timedelta(days=1), datetime.min.time())
    if settings.USE_TZ:
        zona = timezone.get_current_timezone()
        return timezone.make_aware(inicio_naive, zona), timezone.make_aware(fin_naive, zona)
    return inicio_naive, fin_naive


def _detalles_orden_optimizados():
    """Detalles de orden con cantidad recibida calculada en PostgreSQL.

    Evita el patrón N+1 de ejecutar SUM(recepciones) por cada línea de una
    orden. No cambia la fuente de verdad: las cantidades siguen saliendo de
    DetalleRecepcion.
    """
    return (
        DetalleOrdenCompra.objects.select_related('producto')
        .annotate(
            cantidad_recibida_calc=Coalesce(
                models.Sum('recepciones__cantidad_recibida'),
                models.Value(0),
                output_field=models.IntegerField(),
            )
        )
    )


def _detalles_pedido_optimizados():
    """Detalles de pedido con cantidad facturada calculada en una consulta."""
    return (
        DetallePedidoCliente.objects.select_related('producto')
        .annotate(
            cantidad_facturada_calc=Coalesce(
                models.Sum('ventas__cantidad'),
                models.Value(0),
                output_field=models.IntegerField(),
            )
        )
    )


def _normalizar_texto(valor):
    """Normaliza texto para comparar valores como Nómina/Nomina sin depender de tildes."""
    import unicodedata
    texto = str(valor or '').casefold()
    return ''.join(
        caracter for caracter in unicodedata.normalize('NFKD', texto)
        if not unicodedata.combining(caracter)
    )


def _es_movimiento_nomina(movimiento):
    """Indica si un movimiento financiero corresponde a un pago de nómina."""
    return 'nomina' in _normalizar_texto(getattr(movimiento, 'categoria', ''))


def _pagos_nomina_reales(desde, hasta):
    """Pagos de nómina guardados realmente en Finanzas dentro del periodo."""
    movimientos = MovimientoFinanciero.objects.filter(
        tipo='Gasto', categoria__icontains='mina',
        fecha__gte=desde, fecha__lte=hasta
    ).order_by('-fecha', '-id')
    return list(movimientos)


def _pago_empleado_en_mes(empleado, fecha_mes):
    """Devuelve el total pagado a un empleado durante el mes indicado."""
    inicio = fecha_mes.replace(day=1)
    ultimo = inicio.replace(day=calendar.monthrange(inicio.year, inicio.month)[1])
    nombre = _normalizar_texto(empleado.nombre)
    pagos = [
        m for m in _pagos_nomina_reales(inicio, ultimo)
        if (
            str(getattr(m, 'factura', '') or '').startswith(f'NOM-{empleado.id}-')
            or (nombre and _normalizar_texto(m.cliente_proveedor) == nombre)
        )
    ]
    return sum((m.monto for m in pagos), Decimal('0')), pagos


def _estado_nomina_mes(empleados, inicio_mes, hoy):
    """Relaciona empleados con el estado de la nómina del mes.

    Si ya existe una corrida ``Nomina`` para el mes, usa sus
    ``DetalleNomina`` como snapshot y su estado como fuente de verdad. Así
    Contabilidad no vuelve a mostrar como pendiente una nómina ya pagada por
    el flujo colectivo de RRHH. Para meses anteriores sin corrida generada,
    conserva la compatibilidad con el antiguo pago por empleado.
    """
    nomina = Nomina.objects.filter(fecha_inicio=inicio_mes.replace(day=1)).first()
    if nomina is not None:
        detalles = {
            d.empleado_id: d
            for d in nomina.detalles.select_related('empleado').all()
        }
        resultado = []
        for empleado in empleados:
            detalle = detalles.get(empleado.id)
            if detalle is None:
                resultado.append({
                    'empleado': empleado,
                    'estado_pago': 'Fuera de la corrida',
                    'fecha_pago': None,
                    'pagado': Decimal('0'),
                    'pendiente': Decimal('0'),
                })
                continue
            pagado = detalle.salario_neto if nomina.estado == 'Pagada' else Decimal('0')
            pendiente = Decimal('0') if nomina.estado == 'Pagada' else detalle.salario_neto
            resultado.append({
                'empleado': empleado,
                'estado_pago': 'Pagado' if nomina.estado == 'Pagada' else 'Pendiente',
                'fecha_pago': nomina.fecha_pago,
                'pagado': pagado,
                'pendiente': pendiente,
            })
        return (
            resultado,
            nomina.total_neto if nomina.estado == 'Pagada' else Decimal('0'),
            Decimal('0') if nomina.estado == 'Pagada' else nomina.total_neto,
        )

    movimientos_nomina = _pagos_nomina_reales(inicio_mes, hoy)
    resultado = []
    total_pagado = Decimal('0')
    total_pendiente = Decimal('0')

    for empleado in empleados:
        nombre = _normalizar_texto(empleado.nombre)
        relacionados = [
            m for m in movimientos_nomina
            if not getattr(m, 'nomina_id', None) and (
                str(getattr(m, 'factura', '') or '').startswith(f'NOM-{empleado.id}-')
                or (nombre and _normalizar_texto(m.cliente_proveedor) == nombre)
            )
        ]
        pagado_empleado = sum((m.monto for m in relacionados), Decimal('0'))
        pagado_aplicado = min(pagado_empleado, empleado.salario)
        pendiente_empleado = max(empleado.salario - pagado_aplicado, Decimal('0'))
        fecha_pago = relacionados[0].fecha if relacionados else None

        total_pagado += pagado_aplicado
        total_pendiente += pendiente_empleado
        resultado.append({
            'empleado': empleado,
            'estado_pago': 'Pagado' if pendiente_empleado <= 0 else 'Pendiente',
            'fecha_pago': fecha_pago,
            'pagado': pagado_aplicado,
            'pendiente': pendiente_empleado,
        })

    return resultado, total_pagado, total_pendiente


def _movimientos_nomina(desde, hasta):
    """Genera gastos virtuales de nómina todavía pendientes.

    Optimización para PostgreSQL remoto: las corridas ``Nomina`` del período
    se obtienen en UNA sola consulta. Solo si algún mes no tiene una corrida
    histórica se cargan empleados y pagos antiguos para ejecutar el fallback
    de compatibilidad. La lógica financiera es la misma que antes.
    """
    movimientos = []
    meses = _meses_en_periodo(desde, hasta)
    if not meses:
        return movimientos

    primer_mes = meses[0].replace(day=1)
    ultimo_mes = meses[-1].replace(day=1)
    nominas_por_mes = {
        n.fecha_inicio: n
        for n in Nomina.objects.filter(
            fecha_inicio__gte=primer_mes, fecha_inicio__lte=ultimo_mes
        )
    }

    meses_sin_nomina = [m for m in meses if m.replace(day=1) not in nominas_por_mes]

    # Si todas las mensualidades ya usan el sistema nuevo, no necesitamos
    # consultar empleados ni pagos antiguos en absoluto.
    empleados = []
    pagado_por_mes_empleado = defaultdict(lambda: Decimal('0'))
    if meses_sin_nomina:
        empleados = list(Empleado.objects.filter(estado='Activo').order_by('nombre'))
        empleados_por_id = {e.id: e for e in empleados}
        empleados_por_nombre = {_normalizar_texto(e.nombre): e for e in empleados}
        pagos = _pagos_nomina_reales(desde, hasta)

        for pago in pagos:
            if getattr(pago, 'nomina_id', None):
                continue
            empleado = None
            referencia = str(getattr(pago, 'factura', '') or '')
            if referencia.startswith('NOM-'):
                partes = referencia.split('-')
                if len(partes) >= 3:
                    try:
                        empleado = empleados_por_id.get(int(partes[1]))
                    except (TypeError, ValueError):
                        empleado = None
            if empleado is None:
                empleado = empleados_por_nombre.get(_normalizar_texto(pago.cliente_proveedor))
            if empleado is not None:
                pagado_por_mes_empleado[(pago.fecha.year, pago.fecha.month, empleado.id)] += pago.monto

    for mes in meses:
        clave_mes = mes.replace(day=1)
        nomina = nominas_por_mes.get(clave_mes)
        if nomina is not None:
            if nomina.estado != 'Pagada' and nomina.total_neto > 0:
                movimientos.append(SimpleNamespace(
                    fecha=nomina.fecha_inicio,
                    tipo='Gasto',
                    categoria='Nómina pendiente',
                    cliente_proveedor=f'Nómina {nomina.fecha_inicio.strftime("%m/%Y")}',
                    monto=nomina.total_neto,
                    medio_pago='',
                    factura=f'NOM-{nomina.pk:06d}',
                    es_virtual=True,
                ))
            continue

        # Fallback para datos anteriores a Nomina/DetalleNomina.
        for empleado in empleados:
            pagado = pagado_por_mes_empleado[(mes.year, mes.month, empleado.id)]
            pendiente = max(empleado.salario - pagado, Decimal('0'))
            if pendiente <= 0:
                continue
            movimientos.append(SimpleNamespace(
                fecha=mes,
                tipo='Gasto',
                categoria='Nómina pendiente',
                cliente_proveedor=empleado.nombre,
                monto=pendiente,
                medio_pago='',
                factura='Pendiente',
                es_virtual=True,
            ))
    return movimientos


def _nomina_periodo(desde, hasta):
    """Saldo de nómina aún pendiente dentro de un periodo, sin duplicar pagos reales."""
    return sum((m.monto for m in _movimientos_nomina(desde, hasta)), Decimal('0'))


def _gastos_con_nomina(desde=None, hasta=None):
    """Gastos reales + saldo pendiente de nómina, sin duplicar pagos ya registrados."""
    if desde is None or hasta is None:
        hoy = now().date()
        desde = hoy.replace(day=1)
        hasta = hoy
    reales = list(MovimientoFinanciero.objects.filter(
        tipo='Gasto', fecha__gte=desde, fecha__lte=hasta
    ))
    return reales + _movimientos_nomina(desde, hasta)


def _ventas_credito_con_saldo(cliente=None):
    """Ventas a crédito todavía pendientes, calculadas desde Cobro/CxC.

    Se mantiene esta función porque Contabilidad y la ficha del cliente ya la
    usan. La fuente de verdad nueva es CuentaPorCobrar -> Cobro; los
    MovimientoFinanciero siguen siendo la representación de caja/banco.
    """
    money_field = models.DecimalField(max_digits=14, decimal_places=2)
    queryset = Venta.objects.filter(condicion_pago='Credito').select_related('cliente')
    if cliente is not None:
        queryset = queryset.filter(cliente=cliente)
    return queryset.annotate(
        monto_pagado_calc=Coalesce(
            models.Sum('cuenta_por_cobrar__cobros__monto'),
            models.Value(Decimal('0'), output_field=money_field),
            output_field=money_field,
        )
    ).annotate(
        saldo_pendiente_calc=models.ExpressionWrapper(
            models.F('total') - models.F('monto_pagado_calc'),
            output_field=money_field,
        )
    ).filter(saldo_pendiente_calc__gt=0).order_by('fecha')


def _total_cuentas_por_cobrar(cliente=None):
    ventas = _ventas_credito_con_saldo(cliente)
    return ventas.aggregate(total=models.Sum('saldo_pendiente_calc'))['total'] or Decimal('0')


def _cuenta_automatica_por_medio(medio_pago):
    """Sugiere la cuenta financiera apropiada sin crear cuentas ocultamente."""
    medio = _normalizar_texto(medio_pago)
    if 'tarjeta' in medio:
        return CuentaBancaria.objects.filter(activa=True, tipo='Tarjeta').first()
    if medio in {'efectivo', 'caja chica'}:
        return CuentaBancaria.objects.filter(activa=True, tipo='Caja').first()
    return CuentaBancaria.objects.filter(activa=True, tipo='Banco').first()


def _resolver_tipo_comprobante(cliente, solicitado=None):
    """Resuelve E32/E31/E44 independientemente de la condición de pago."""
    solicitado = str(solicitado or '').strip().upper()
    validos = {'E31', 'E32', 'E44'}
    if solicitado and solicitado != 'AUTO':
        if solicitado not in validos:
            raise ValueError('Tipo de comprobante fiscal inválido.')
        if solicitado == 'E44' and (cliente is None or cliente.condicion_fiscal != 'Exento'):
            raise ValueError('E44 requiere un cliente marcado como Exento / Régimen especial.')
        if solicitado == 'E31' and cliente is None:
            raise ValueError('E31 requiere seleccionar un cliente registrado.')
        return solicitado
    if cliente is not None and cliente.condicion_fiscal == 'Exento':
        return 'E44'
    if cliente is not None and cliente.tipo == 'Empresa':
        return 'E31'
    return 'E32'


def _snapshot_cliente(cliente):
    if cliente is None:
        return {
            'cliente_nombre': 'Cliente Genérico', 'cliente_rnc_cedula': '',
            'cliente_telefono': '', 'cliente_direccion': '', 'cliente_email': '',
        }
    return {
        'cliente_nombre': cliente.nombre,
        'cliente_rnc_cedula': cliente.rnc_cedula,
        'cliente_telefono': cliente.telefono,
        'cliente_direccion': cliente.direccion,
        'cliente_email': cliente.email,
    }


def _calcular_linea_fiscal(producto, cantidad, precio, descuento_pct, cliente, tasa_empresa):
    importe_bruto = (precio * cantidad).quantize(Decimal('0.01'))
    descuento = (importe_bruto * descuento_pct / Decimal('100')).quantize(Decimal('0.01'))
    subtotal = importe_bruto - descuento
    exenta_cliente = cliente is not None and cliente.condicion_fiscal == 'Exento'
    tasa = Decimal('0') if (producto.exento_itbis or exenta_cliente) else tasa_empresa
    itbs = (subtotal * tasa / Decimal('100')).quantize(Decimal('0.01'))
    return {
        'importe_bruto': importe_bruto,
        'descuento': descuento,
        'subtotal': subtotal,
        'tasa_itbs': tasa,
        'itbs': itbs,
    }


@login_required
@requiere_permiso('dashboard')
def dashboard(request):
    hoy = now().date()
    inicio_mes_actual = hoy.replace(day=1)
    fin_mes_anterior = inicio_mes_actual - timedelta(days=1)
    inicio_mes_anterior = fin_mes_anterior.replace(day=1)

    # Una sola consulta para los dos totales financieros globales.
    money_field = models.DecimalField(max_digits=16, decimal_places=2)
    totales_fin = MovimientoFinanciero.objects.aggregate(
        ingresos=Coalesce(
            models.Sum('monto', filter=models.Q(tipo='Ingreso')),
            models.Value(Decimal('0'), output_field=money_field),
            output_field=money_field,
        ),
        gastos=Coalesce(
            models.Sum('monto', filter=models.Q(tipo='Gasto')),
            models.Value(Decimal('0'), output_field=money_field),
            output_field=money_field,
        ),
    )
    total_ingresos = totales_fin['ingresos']
    total_gastos_reales = totales_fin['gastos']

    # Se calcula una sola vez y se reutiliza tanto en el KPI como en los
    # movimientos recientes. Antes la misma nómina virtual disparaba dos
    # bloques de consultas por cada visita al Dashboard.
    nomina_virtual_mes = _movimientos_nomina(inicio_mes_actual, hoy)
    total_gastos = total_gastos_reales + sum(
        (m.monto for m in nomina_virtual_mes), Decimal('0')
    )
    ganancia_neta = total_ingresos - total_gastos

    # Mes actual y anterior en una sola consulta. Los límites directos sobre
    # ``fecha`` permiten aprovechar el índice del DateTimeField.
    inicio_actual_dt, fin_actual_dt = _rango_datetime(inicio_mes_actual, hoy)
    inicio_anterior_dt, fin_anterior_dt = _rango_datetime(inicio_mes_anterior, fin_mes_anterior)
    ventas_periodos = Venta.objects.aggregate(
        actual=Coalesce(
            models.Sum(
                'total',
                filter=models.Q(fecha__gte=inicio_actual_dt, fecha__lt=fin_actual_dt),
            ),
            models.Value(Decimal('0'), output_field=money_field),
            output_field=money_field,
        ),
        anterior=Coalesce(
            models.Sum(
                'total',
                filter=models.Q(fecha__gte=inicio_anterior_dt, fecha__lt=fin_anterior_dt),
            ),
            models.Value(Decimal('0'), output_field=money_field),
            output_field=money_field,
        ),
    )
    total_ventas_mes = ventas_periodos['actual']
    total_ventas_mes_anterior = ventas_periodos['anterior']
    tendencia_ventas_mes, ventas_mes_positiva = _tendencia(
        total_ventas_mes, total_ventas_mes_anterior
    )

    movimientos = list(MovimientoFinanciero.objects.all()[:5])
    movimientos.extend(nomina_virtual_mes)
    movimientos.sort(key=lambda m: m.fecha, reverse=True)

    grafica_categorias = _grafica_ventas_por_categoria(hoy)

    return render(request, 'core/dashboard.html', {
        'movimientos': movimientos[:8],
        'total_ingresos': total_ingresos,
        'total_gastos': total_gastos,
        'ganancia_neta': ganancia_neta,
        'total_ventas_mes': total_ventas_mes,
        'tendencia_ventas_mes': tendencia_ventas_mes,
        'ventas_mes_positiva': ventas_mes_positiva,
        'nomina_mensual': _nomina_mensual_actual(),
        'grafica_categorias': grafica_categorias,
    })


@login_required
@requiere_permiso('productos')
def productos(request):
    puede_crear = puede_escribir_en(request, 'productos')
    editando = False
    if request.method == 'POST':
        accion = request.POST.get('accion', 'crear')
        producto_id = request.POST.get('producto_id')
        editando = accion == 'editar' and bool(producto_id)
        if not puede_crear:
            messages.error(request, 'No tienes permiso para modificar productos.')
            return redirect('productos')
        if accion == 'eliminar' and producto_id:
            producto = get_object_or_404(Producto, pk=producto_id)
            if MovimientoStock.objects.filter(producto=producto).exists() or DetalleVenta.objects.filter(producto=producto).exists():
                messages.error(request, 'No puedes eliminar un producto que ya tiene movimientos o ventas.')
            else:
                producto.delete()
                messages.success(request, 'Producto eliminado correctamente.')
            return redirect('productos')
        if accion == 'editar' and producto_id:
            producto = get_object_or_404(Producto, pk=producto_id)
            form = ProductoForm(request.POST, instance=producto)
        else:
            form = ProductoForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Producto actualizado correctamente.' if accion == 'editar' else 'Producto creado correctamente.')
            return redirect('productos')
    else:
        producto_id = request.GET.get('editar')
        editando = bool(producto_id)
        form = ProductoForm(instance=get_object_or_404(Producto, pk=producto_id)) if producto_id else ProductoForm()
    lista_productos = Producto.objects.all().order_by('nombre')
    # El template muestra mov.producto.nombre. Sin select_related Django hacía
    # una consulta adicional por cada fila (N+1), muy costoso con Neon remoto.
    movimientos = MovimientoStock.objects.select_related('producto').all()[:20]
    return render(request, 'core/productos.html', {
        'productos': lista_productos, 'form': form, 'movimientos': movimientos,
        'puede_crear': puede_crear, 'editando': editando,
    })



@login_required
@requiere_permiso('ventas')
def ventas(request):
    # Evaluar una sola vez evita dos viajes a la BD remota para el mismo catálogo.
    productos = list(Producto.objects.all())
    historial = [
        {'sku': p.sku, 'producto': p.nombre, 'precio': float(p.precio_venta)}
        for p in productos
    ]
    catalogo_json = [
        {
            'sku': p.sku,
            'nombre': p.nombre,
            'precio': float(p.precio_venta),
            'stock': p.stock,
            'stock_minimo': p.stock_minimo,
            'exento_itbis': p.exento_itbis,
        }
        for p in productos
    ]
    clientes = list(Cliente.objects.filter(activo=True).order_by('nombre'))
    clientes_json = [
        {
            'id': c.id, 'codigo': c.codigo, 'nombre': c.nombre,
            'tipo': c.tipo, 'condicion_fiscal': c.condicion_fiscal,
            'rnc_cedula': c.rnc_cedula,
        }
        for c in clientes
    ]
    # No se consultan ventas/pedidos recientes aquí: el template de Ventas no
    # consume esos QuerySets. Eliminarlos evita viajes a Neon sin cambiar nada
    # de lo que se muestra ni de lo que puede hacer el usuario.
    return render(request, 'core/ventas.html', {
        'historial_json': historial,
        'catalogo_json': catalogo_json,
        'clientes_json': clientes_json,
        'itbs_porcentaje_json': float(DatosEmpresa.obtener().itbs_porcentaje),
        'productos_lista': productos,
        'clientes_lista': clientes,
        'puede_crear': puede_escribir_en(request, 'ventas'),
        'puede_crear_clientes': puede_escribir_en(request, 'clientes'),
    })


@login_required
@requiere_permiso('finanzas')
def finanzas(request):
    """Módulo financiero.

    Los INGRESOS son de solo lectura y se generan exclusivamente desde los
    flujos reales del sistema (Ventas y cobros de CxC). Aquí solo se permite
    registrar manualmente GASTOS operativos y administrar cuentas financieras.

    Rendimiento: cada pestaña consulta únicamente sus propios datos. Antes la
    vista cargaba Ingresos + Gastos + Historial + CxC + CxP + Cuentas en cada
    visita, aunque el usuario solo estuviera viendo una pestaña. Esto era
    especialmente lento usando PostgreSQL/Neon remoto.
    """
    puede_crear = puede_escribir_en(request, 'finanzas')
    puede_ver_cxc = request.user.has_perm('core.ver_cuentas_por_cobrar')
    puede_cobrar = request.user.has_perm('core.registrar_cobros')
    puede_ver_cxp = request.user.has_perm('core.ver_compras')
    puede_pagar_cxp = request.user.has_perm('core.registrar_pagos_compra')

    tabs_validas = {'ingresos', 'gastos', 'historial', 'cuentas'}
    if puede_ver_cxc:
        tabs_validas.add('cxc')
    if puede_ver_cxp:
        tabs_validas.add('cxp')

    tab_activa = request.GET.get('tab', 'ingresos').strip().lower()
    if tab_activa not in tabs_validas:
        tab_activa = 'ingresos'

    form = MovimientoFinancieroForm()
    form_cuenta = CuentaBancariaForm()

    if request.method == 'POST':
        accion = request.POST.get('accion', '').strip()

        if accion == 'agregar_cuenta':
            if not puede_crear:
                messages.error(request, 'No tienes permiso para administrar cuentas financieras.')
                return redirect('finanzas')
            form_cuenta = CuentaBancariaForm(request.POST)
            if form_cuenta.is_valid():
                form_cuenta.save()
                messages.success(request, 'Cuenta financiera agregada correctamente.')
                return redirect(f"{reverse('finanzas')}?tab=cuentas")
            tab_activa = 'cuentas'

        elif accion == 'registrar_gasto':
            if not puede_crear:
                messages.error(request, 'No tienes permiso para registrar gastos.')
                return redirect('finanzas')

            # El tipo nunca se toma del navegador: Finanzas solo admite
            # gastos manuales. Los ingresos nacen de Ventas/CxC.
            datos = request.POST.copy()
            datos['tipo'] = 'Gasto'
            form = MovimientoFinancieroForm(datos)
            tab_activa = 'gastos'

            if form.is_valid():
                movimiento = form.save(commit=False)
                if movimiento.monto <= 0:
                    form.add_error('monto', 'El monto debe ser mayor que cero.')
                elif 'nomina' in _normalizar_texto(movimiento.categoria):
                    form.add_error(
                        'categoria',
                        'Los pagos de nómina deben registrarse desde RRHH → Nómina.'
                    )
                else:
                    movimiento.tipo = 'Gasto'
                    movimiento.save()
                    messages.success(
                        request,
                        f'Gasto registrado correctamente por RD$ {movimiento.monto:,.2f}.'
                    )
                    return redirect(f"{reverse('finanzas')}?tab=gastos")
        else:
            # Bloquea también POST antiguos/manipulados que intenten enviar
            # tipo=Ingreso directamente a /finanzas/.
            messages.error(
                request,
                'Los ingresos no se registran manualmente en Finanzas. '
                'Se generan automáticamente desde Ventas y Cuentas por Cobrar.'
            )
            return redirect(f"{reverse('finanzas')}?tab=ingresos")

    hoy = now().date()
    inicio_mes = hoy.replace(day=1)

    # Valores vacíos por defecto: solo se llena lo que corresponde a la
    # pestaña solicitada para evitar decenas de consultas innecesarias.
    ingresos = []
    gastos = []
    historial = []
    total_ingresos = Decimal('0')
    total_gastos = Decimal('0')
    cuentas_por_cobrar = []
    clientes_con_cxc = []
    total_por_cobrar = Decimal('0')
    cuentas_vencidas = 0
    cuentas_por_pagar = []
    proveedores_con_cxp = []
    total_por_pagar = Decimal('0')
    pagos_vencidos = 0
    cuentas_financieras = []

    if tab_activa == 'ingresos':
        ingresos = list(
            MovimientoFinanciero.objects.filter(tipo='Ingreso')
            .select_related('cuenta')
        )
        total_ingresos = sum((m.monto for m in ingresos), Decimal('0'))

    elif tab_activa == 'gastos':
        gastos_reales = list(
            MovimientoFinanciero.objects.filter(tipo='Gasto')
            .select_related('cuenta')
        )
        nomina_virtual = _movimientos_nomina(inicio_mes, hoy)
        gastos = gastos_reales + nomina_virtual
        gastos.sort(key=lambda m: m.fecha, reverse=True)
        total_gastos = sum((m.monto for m in gastos_reales), Decimal('0')) + sum(
            (m.monto for m in nomina_virtual), Decimal('0')
        )

    elif tab_activa == 'historial':
        nomina_virtual = _movimientos_nomina(inicio_mes, hoy)
        historial = list(
            MovimientoFinanciero.objects.select_related('cuenta').all()
        ) + nomina_virtual
        historial.sort(key=lambda m: m.fecha, reverse=True)

    elif tab_activa == 'cxc' and puede_ver_cxc:
        money_field = models.DecimalField(max_digits=14, decimal_places=2)
        cuentas_qs = (
            CuentaPorCobrar.objects
            .select_related('cliente', 'venta')
            .annotate(
                total_cobrado_calc=Coalesce(
                    models.Sum('cobros__monto'),
                    models.Value(Decimal('0'), output_field=money_field),
                    output_field=money_field,
                )
            )
        )
        cliente_filtro = request.GET.get('cxc_cliente', '').strip()
        vence_desde = request.GET.get('cxc_vence_desde', '').strip()
        vence_hasta = request.GET.get('cxc_vence_hasta', '').strip()
        buscar = request.GET.get('cxc_buscar', '').strip()
        if cliente_filtro:
            cuentas_qs = cuentas_qs.filter(cliente_id=cliente_filtro)
        if vence_desde:
            cuentas_qs = cuentas_qs.filter(fecha_vencimiento__gte=vence_desde)
        if vence_hasta:
            cuentas_qs = cuentas_qs.filter(fecha_vencimiento__lte=vence_hasta)
        if buscar:
            cuentas_qs = cuentas_qs.filter(
                models.Q(cliente__nombre__icontains=buscar) |
                models.Q(venta__numero_factura__icontains=buscar)
            )

        cuentas_por_cobrar = list(cuentas_qs)
        for cuenta in cuentas_por_cobrar:
            cuenta.saldo_pendiente_calc = max(
                cuenta.importe_original - cuenta.total_cobrado_calc, Decimal('0')
            )
            if cuenta.saldo_pendiente_calc <= 0:
                cuenta.estado_calc = CuentaPorCobrar.ESTADO_PAGADA
            elif cuenta.fecha_vencimiento < hoy:
                cuenta.estado_calc = CuentaPorCobrar.ESTADO_VENCIDA
            else:
                cuenta.estado_calc = CuentaPorCobrar.ESTADO_PENDIENTE

        estado_filtro = request.GET.get('cxc_estado', '').strip()
        if estado_filtro in dict(CuentaPorCobrar.ESTADO_CHOICES):
            cuentas_por_cobrar = [
                c for c in cuentas_por_cobrar if c.estado_calc == estado_filtro
            ]
        total_por_cobrar = sum(
            (c.saldo_pendiente_calc for c in cuentas_por_cobrar if c.saldo_pendiente_calc > 0),
            Decimal('0')
        )
        cuentas_vencidas = sum(
            1 for c in cuentas_por_cobrar
            if c.estado_calc == CuentaPorCobrar.ESTADO_VENCIDA
        )
        clientes_con_cxc = Cliente.objects.filter(
            cuentas_por_cobrar__isnull=False
        ).distinct().order_by('nombre')

    elif tab_activa == 'cxp' and puede_ver_cxp:
        compras_qs = Compra.objects.select_related('proveedor').filter(
            condicion_pago='Credito'
        )
        proveedor_filtro = request.GET.get('cxp_proveedor', '').strip()
        vence_desde_cxp = request.GET.get('cxp_vence_desde', '').strip()
        vence_hasta_cxp = request.GET.get('cxp_vence_hasta', '').strip()
        buscar_cxp = request.GET.get('cxp_buscar', '').strip()
        if proveedor_filtro:
            compras_qs = compras_qs.filter(proveedor_id=proveedor_filtro)
        if vence_desde_cxp:
            compras_qs = compras_qs.filter(fecha_vencimiento__gte=vence_desde_cxp)
        if vence_hasta_cxp:
            compras_qs = compras_qs.filter(fecha_vencimiento__lte=vence_hasta_cxp)
        if buscar_cxp:
            compras_qs = compras_qs.filter(
                models.Q(proveedor__nombre__icontains=buscar_cxp) |
                models.Q(numero_factura__icontains=buscar_cxp)
            )
        cuentas_por_pagar = list(compras_qs)
        estado_filtro_cxp = request.GET.get('cxp_estado', '').strip()
        if estado_filtro_cxp in ('Pendiente', 'Parcial', 'Vencida', 'Pagada'):
            cuentas_por_pagar = [
                c for c in cuentas_por_pagar if c.estado_cxp == estado_filtro_cxp
            ]
        total_por_pagar = sum(
            (c.saldo_pendiente for c in cuentas_por_pagar if c.saldo_pendiente > 0),
            Decimal('0')
        )
        pagos_vencidos = sum(
            1 for c in cuentas_por_pagar if c.estado_cxp == 'Vencida'
        )
        proveedores_con_cxp = Proveedor.objects.filter(
            compras__condicion_pago='Credito'
        ).distinct().order_by('nombre')

    elif tab_activa == 'cuentas':
        money_field = models.DecimalField(max_digits=14, decimal_places=2)
        cuentas_financieras = list(
            CuentaBancaria.objects.filter(activa=True)
            .annotate(
                total_ingresos_calc=Coalesce(
                    models.Sum(
                        'movimientos__monto',
                        filter=models.Q(movimientos__tipo='Ingreso'),
                    ),
                    models.Value(Decimal('0'), output_field=money_field),
                    output_field=money_field,
                ),
                total_gastos_calc=Coalesce(
                    models.Sum(
                        'movimientos__monto',
                        filter=models.Q(movimientos__tipo='Gasto'),
                    ),
                    models.Value(Decimal('0'), output_field=money_field),
                    output_field=money_field,
                ),
            )
            .annotate(
                saldo_calc=models.ExpressionWrapper(
                    models.F('total_ingresos_calc') - models.F('total_gastos_calc'),
                    output_field=money_field,
                )
            )
            .order_by('nombre')
        )

    return render(request, 'core/finanzas.html', {
        'form': form,
        'ingresos': ingresos,
        'gastos': gastos,
        'historial': historial,
        'total_ingresos': total_ingresos,
        'total_gastos': total_gastos,
        'puede_crear': puede_crear,
        'puede_ver_cxc': puede_ver_cxc,
        'puede_cobrar': puede_cobrar,
        'cuentas_por_cobrar': cuentas_por_cobrar,
        'clientes_con_cxc': clientes_con_cxc,
        'total_por_cobrar': total_por_cobrar,
        'cuentas_vencidas': cuentas_vencidas,
        'estado_cxc_choices': CuentaPorCobrar.ESTADO_CHOICES,
        'puede_ver_cxp': puede_ver_cxp,
        'puede_pagar_cxp': puede_pagar_cxp,
        'cuentas_por_pagar': cuentas_por_pagar,
        'proveedores_con_cxp': proveedores_con_cxp,
        'total_por_pagar': total_por_pagar,
        'pagos_vencidos': pagos_vencidos,
        'cuentas_financieras': cuentas_financieras,
        'form_cuenta': form_cuenta,
        'tab_activa': tab_activa,
        'hoy': hoy,
    })

def _venta_relacionada_con_ingreso(movimiento):
    """Devuelve la Venta asociada al ingreso sin depender del texto de factura.

    Los movimientos automáticos de ventas/CxC ya guardan ``venta``. Para
    movimientos históricos anteriores se intenta resolver por numero_factura.
    """
    if getattr(movimiento, 'venta_id', None):
        return Venta.objects.filter(pk=movimiento.venta_id).prefetch_related('detalles__producto').first()
    referencia = (movimiento.factura or '').strip()
    if referencia:
        return Venta.objects.filter(numero_factura=referencia).prefetch_related('detalles__producto').first()
    return None


def _generar_pdf_recibo_ingreso(movimiento, empresa):
    venta = _venta_relacionada_con_ingreso(movimiento)
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=1.6*cm, bottomMargin=1.6*cm)
    estilos = getSampleStyleSheet()
    oscuro = colors.HexColor('#0f1524')
    acento = colors.HexColor('#21c99a')
    titulo = ParagraphStyle('ReciboIngresoTitulo', parent=estilos['Normal'], fontSize=18, fontName='Helvetica-Bold', textColor=acento, alignment=2)
    normal = ParagraphStyle('ReciboIngresoNormal', parent=estilos['Normal'], fontSize=9, textColor=oscuro, leading=12)

    empresa_txt = [empresa.nombre_comercial]
    if empresa.rnc:
        empresa_txt.append(f'RNC: {empresa.rnc}')
    if empresa.direccion:
        empresa_txt.append(empresa.direccion)
    encabezado = Table([[
        Paragraph('<br/>'.join(empresa_txt), normal),
        Paragraph(f'RECIBO DE INGRESO<br/>REC-{movimiento.pk:06d}', titulo),
    ]], colWidths=[10.5*cm, 6.5*cm])
    encabezado.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LINEBELOW',(0,0),(-1,0),1.2,oscuro)]))
    elementos = [encabezado, Spacer(1, 0.4*cm)]
    meta = [
        ['Fecha', movimiento.fecha.strftime('%d/%m/%Y')],
        ['Recibido de', movimiento.cliente_proveedor or '--'],
        ['Medio de pago', movimiento.medio_pago or '--'],
        ['Cuenta', movimiento.cuenta.nombre if movimiento.cuenta_id else '--'],
        ['Referencia', movimiento.factura or '--'],
    ]
    tabla_meta = Table(meta, colWidths=[4*cm, 12*cm])
    tabla_meta.setStyle(TableStyle([('FONTSIZE',(0,0),(-1,-1),9),('FONTNAME',(0,0),(0,-1),'Helvetica-Bold')]))
    elementos.extend([tabla_meta, Spacer(1, 0.45*cm)])

    if venta:
        filas = [['Producto','Cant.','Precio base','Subtotal']]
        for d in venta.detalles.all():
            filas.append([d.producto.nombre, str(d.cantidad), f'RD$ {d.precio_unitario:,.2f}', f'RD$ {d.subtotal:,.2f}'])
        tabla = Table(filas, colWidths=[7*cm,2*cm,3.5*cm,3.5*cm], repeatRows=1)
        tabla.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),oscuro),('TEXTCOLOR',(0,0),(-1,0),colors.white),
            ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,-1),8.5),
            ('GRID',(0,0),(-1,-1),0.35,colors.HexColor('#E5E7EB')),('ALIGN',(1,1),(-1,-1),'RIGHT'),
        ]))
        elementos.extend([tabla, Spacer(1,0.35*cm)])
        elementos.append(Paragraph(
            f'Venta relacionada: {venta.numero_factura}. Total de la venta: RD$ {venta.total:,.2f}.',
            normal,
        ))
        elementos.append(Spacer(1,0.2*cm))

    total = Table([['MONTO RECIBIDO', f'RD$ {movimiento.monto:,.2f}']], colWidths=[11*cm,5*cm], hAlign='RIGHT')
    total.setStyle(TableStyle([('FONTNAME',(0,0),(-1,-1),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,-1),12),('LINEABOVE',(0,0),(-1,0),1.2,oscuro),('ALIGN',(1,0),(1,0),'RIGHT')]))
    elementos.append(total)
    doc.build(elementos)
    buffer.seek(0)
    return buffer


@login_required
@requiere_permiso('finanzas')
def recibo_ingreso(request, movimiento_id):
    movimiento = get_object_or_404(
        MovimientoFinanciero.objects.select_related('cuenta', 'venta'),
        pk=movimiento_id, tipo='Ingreso',
    )
    return render(request, 'core/recibo_ingreso.html', {
        'movimiento': movimiento,
        'empresa': DatosEmpresa.obtener(),
        'venta': _venta_relacionada_con_ingreso(movimiento),
    })


@login_required
@requiere_permiso('finanzas')
def recibo_ingreso_pdf(request, movimiento_id):
    movimiento = get_object_or_404(
        MovimientoFinanciero.objects.select_related('cuenta', 'venta'),
        pk=movimiento_id, tipo='Ingreso',
    )
    buffer = _generar_pdf_recibo_ingreso(movimiento, DatosEmpresa.obtener())
    response = HttpResponse(buffer.read(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="REC-{movimiento.pk:06d}.pdf"'
    return response


@login_required
@permission_required('core.ver_cuentas_por_cobrar', raise_exception=True)
def cuenta_por_cobrar_detalle(request, cuenta_id):
    cuenta = get_object_or_404(
        CuentaPorCobrar.objects.select_related('cliente', 'venta'), pk=cuenta_id
    )
    return render(request, 'core/cuenta_por_cobrar_detalle.html', {
        'cuenta': cuenta,
        'cobros': cuenta.cobros.select_related('registrado_por', 'movimiento_financiero__cuenta').all(),
        'puede_cobrar': request.user.has_perm('core.registrar_cobros'),
        'form': CobroForm(initial={'fecha': now().date()}),
    })


@login_required
@permission_required('core.registrar_cobros', raise_exception=True)
@require_POST
def cuenta_por_cobrar_registrar_cobro(request, cuenta_id):
    form = CobroForm(request.POST)
    if not form.is_valid():
        for errores in form.errors.values():
            for error in errores:
                messages.error(request, error)
        return redirect('cuenta_por_cobrar_detalle', cuenta_id=cuenta_id)

    monto = form.cleaned_data['monto']
    try:
        with transaction.atomic():
            cuenta = CuentaPorCobrar.objects.select_for_update().select_related('venta', 'cliente').get(pk=cuenta_id)
            saldo = cuenta.saldo_pendiente
            if saldo <= 0:
                raise ValueError('Esta cuenta ya está totalmente pagada.')
            if monto > saldo:
                raise ValueError(
                    f'El monto (RD$ {monto:,.2f}) supera el saldo pendiente '
                    f'(RD$ {saldo:,.2f}). No se registró el cobro.'
                )
            cobro = form.save(commit=False)
            cobro.cuenta = cuenta
            cobro.registrado_por = request.user
            cobro.save()
            cuenta_financiera = form.cleaned_data.get('cuenta_financiera') or _cuenta_automatica_por_medio(cobro.metodo_pago)
            MovimientoFinanciero.objects.create(
                tipo='Ingreso', fecha=cobro.fecha, categoria='Cobro CxC',
                cliente_proveedor=cuenta.cliente.nombre, monto=cobro.monto,
                medio_pago=cobro.metodo_pago, cuenta=cuenta_financiera,
                factura=cuenta.venta.numero_factura, venta=cuenta.venta, cobro=cobro,
            )
    except CuentaPorCobrar.DoesNotExist:
        messages.error(request, 'La cuenta por cobrar no existe.')
        return redirect('finanzas')
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('cuenta_por_cobrar_detalle', cuenta_id=cuenta_id)

    messages.success(request, f'Cobro de RD$ {monto:,.2f} registrado.')
    return redirect('cuenta_por_cobrar_detalle', cuenta_id=cuenta_id)


@login_required
@requiere_permiso('inventario')
def inventario(request):
    puede_crear = puede_escribir_en(request, 'inventario')

    if request.method == 'POST':
        if not puede_crear:
            messages.error(request, 'No tienes permiso para registrar movimientos de stock.')
            return redirect('inventario')

        form = MovimientoStockForm(request.POST)
        if form.is_valid():
            movimiento = form.save(commit=False)
            producto = movimiento.producto

            if movimiento.tipo == 'Entrada':
                producto.stock += movimiento.cantidad
            else:  # Salida
                if movimiento.cantidad > producto.stock:
                    form.add_error('cantidad', 'No hay suficiente stock para esta salida.')
                    movimientos_qs = MovimientoStock.objects.select_related('producto')
                    return render(request, 'core/inventario.html', {
                        'productos': Producto.objects.all(),
                        'form': form,
                        'movimientos': movimientos_qs[:20],
                        'historial': movimientos_qs,
                        'puede_crear': puede_crear,
                        **_contexto_kpis(),
                    })
                producto.stock -= movimiento.cantidad

            with transaction.atomic():
                producto.save()
                movimiento.save()
            return redirect('inventario')
    else:
        form = MovimientoStockForm()

    lista_productos = Producto.objects.all()
    # Inventario renderiza el producto relacionado tanto en “Movimientos”
    # como en “Historial”. Cargarlo en JOIN elimina el patrón N+1.
    movimientos_qs = MovimientoStock.objects.select_related('producto').all()
    movimientos = movimientos_qs[:20]

    return render(request, 'core/inventario.html', {
        'productos': lista_productos,
        'form': form,
        'movimientos': movimientos,
        'historial': movimientos_qs,
        'puede_crear': puede_crear,
        **_contexto_kpis(),
    })


def _contexto_kpis():
    money_field = models.DecimalField(max_digits=16, decimal_places=2)
    kpis = Producto.objects.aggregate(
        total_productos=models.Count('id'),
        bajo_stock=models.Count(
            'id', filter=models.Q(stock__lt=models.F('stock_minimo'))
        ),
        valor_total=Coalesce(
            models.Sum(
                models.ExpressionWrapper(
                    models.F('costo_compra') * models.F('stock'),
                    output_field=money_field,
                )
            ),
            models.Value(Decimal('0'), output_field=money_field),
            output_field=money_field,
        ),
    )
    kpis['total_movimientos'] = MovimientoStock.objects.count()
    return kpis


@login_required
@requiere_permiso('clientes')
def clientes(request):
    puede_crear = puede_escribir_en(request, 'clientes')
    cliente_id = request.GET.get('editar')
    if request.method == 'POST':
        if not puede_crear:
            messages.error(request, 'No tienes permiso para modificar clientes.')
            return redirect('clientes')
        accion = request.POST.get('accion', 'crear')
        cid = request.POST.get('cliente_id')
        if accion == 'eliminar' and cid:
            cliente = get_object_or_404(Cliente, pk=cid)
            if Venta.objects.filter(cliente=cliente).exists():
                messages.error(request, 'No puedes eliminar un cliente con ventas asociadas.')
            elif PedidoCliente.objects.filter(cliente=cliente).exists():
                messages.error(request, 'No puedes eliminar un cliente con pedidos asociados.')
            else:
                cliente.delete()
                messages.success(request, 'Cliente eliminado correctamente.')
            return redirect('clientes')
        cliente = get_object_or_404(Cliente, pk=cid) if accion == 'editar' and cid else None
        form = ClienteForm(request.POST, instance=cliente)
        if form.is_valid():
            form.save()
            messages.success(request, 'Cliente actualizado correctamente.' if cliente else 'Cliente creado correctamente.')
            return redirect('clientes')
    else:
        form = ClienteForm(instance=get_object_or_404(Cliente, pk=cliente_id)) if cliente_id else ClienteForm()
    lista_clientes = Cliente.objects.annotate(
        ultima_compra_calc=models.Max('venta__fecha')
    ).order_by('nombre')
    return render(request, 'core/clientes.html', {
        'clientes': lista_clientes, 'form': form,
        'puede_crear': puede_crear, 'editando': bool(cliente_id),
    })


@login_required
@requiere_permiso('clientes')
def clientes_detalle(request, cliente_id):
    """Ficha del cliente: información + historial de compras reales
    (Venta/DetalleVenta) + pedidos pendientes (PedidoCliente). No crea
    ningún modelo de historial: ambas secciones son consultas sobre
    los modelos que ya son la fuente de verdad de cada dato."""
    cliente = get_object_or_404(
        Cliente.objects.annotate(ultima_compra_calc=models.Max('venta__fecha')),
        pk=cliente_id,
    )
    ventas = (
        Venta.objects.filter(cliente=cliente)
        .prefetch_related('detalles__producto')
        .order_by('-fecha')
    )
    pedidos_pendientes = (
        PedidoCliente.objects.filter(cliente=cliente)
        .exclude(estado__in=['Completado', 'Cancelado'])
        .prefetch_related(
            models.Prefetch('detalles', queryset=_detalles_pedido_optimizados())
        )
        .order_by('-fecha_creacion')
    )
    ventas_credito_pendientes = list(_ventas_credito_con_saldo(cliente))
    saldo_por_cobrar = sum(
        (v.saldo_pendiente_calc for v in ventas_credito_pendientes), Decimal('0')
    )
    return render(request, 'core/clientes_detalle.html', {
        'cliente': cliente,
        'ventas': ventas,
        'pedidos_pendientes': pedidos_pendientes,
        'total_comprado': sum((v.total for v in ventas), Decimal('0')),
        'ventas_credito_pendientes': ventas_credito_pendientes,
        'saldo_por_cobrar': saldo_por_cobrar,
    })



@login_required
@requiere_permiso('proyecciones')
def proyecciones(request):
    hoy = now().date()
    inicio_mes = hoy.replace(day=1)
    money_field = models.DecimalField(max_digits=16, decimal_places=2)

    # Ingresos y gastos reales del mes en una sola ida a PostgreSQL.
    totales_mes = MovimientoFinanciero.objects.filter(
        fecha__gte=inicio_mes, fecha__lte=hoy
    ).aggregate(
        ingresos=Coalesce(
            models.Sum('monto', filter=models.Q(tipo='Ingreso')),
            models.Value(Decimal('0'), output_field=money_field),
            output_field=money_field,
        ),
        gastos=Coalesce(
            models.Sum('monto', filter=models.Q(tipo='Gasto')),
            models.Value(Decimal('0'), output_field=money_field),
            output_field=money_field,
        ),
    )
    total_ingresos_mes = totales_mes['ingresos']
    total_gastos_mes = totales_mes['gastos'] + sum(
        (m.monto for m in _movimientos_nomina(inicio_mes, hoy)), Decimal('0')
    )
    clientes_activos = Cliente.objects.count()

    # La proyección mantiene exactamente la misma fórmula, pero PostgreSQL
    # agrupa unidades por producto/mes y ya no se descargan todos los detalles
    # de venta de 90 días para sumarlos en Python.
    fecha_limite = hoy - timedelta(days=90)
    inicio_dt, fin_dt = _rango_datetime(fecha_limite, hoy)
    filas = (
        DetalleVenta.objects
        .filter(venta__fecha__gte=inicio_dt, venta__fecha__lt=fin_dt)
        .annotate(mes=TruncMonth('venta__fecha'))
        .values(
            'producto_id', 'producto__sku', 'producto__nombre',
            'producto__categoria', 'producto__precio_venta', 'mes',
        )
        .annotate(unidades=models.Sum('cantidad'))
        .order_by()
    )

    ventas_por_producto_mes = defaultdict(dict)
    productos_info = {}
    for fila in filas:
        pid = fila['producto_id']
        productos_info[pid] = SimpleNamespace(
            id=pid,
            sku=fila['producto__sku'],
            nombre=fila['producto__nombre'],
            categoria=fila['producto__categoria'],
            precio_venta=fila['producto__precio_venta'],
        )
        clave_mes = (fila['mes'].year, fila['mes'].month)
        ventas_por_producto_mes[pid][clave_mes] = fila['unidades'] or 0

    proyecciones_productos = []
    for producto_id, meses in ventas_por_producto_mes.items():
        producto = productos_info[producto_id]
        cantidades = list(meses.values())
        promedio_mensual = sum(cantidades) / len(cantidades) if cantidades else 0
        proyeccion_unidades = round(promedio_mensual * 1.1)
        proyeccion_monto = proyeccion_unidades * producto.precio_venta
        proyecciones_productos.append({
            'producto': producto,
            'promedio_mensual': round(promedio_mensual, 1),
            'proyeccion_unidades': proyeccion_unidades,
            'proyeccion_monto': proyeccion_monto,
        })

    proyecciones_productos.sort(key=lambda x: x['proyeccion_monto'], reverse=True)
    top_5_proyeccion = proyecciones_productos[:5]
    ALTURA_MAX_PROYECCION_PX = 152
    monto_max_proyeccion = max(
        (item['proyeccion_monto'] for item in top_5_proyeccion), default=0
    )
    for item in top_5_proyeccion:
        item['altura_px'] = (
            int((item['proyeccion_monto'] / monto_max_proyeccion) * ALTURA_MAX_PROYECCION_PX)
            if monto_max_proyeccion else 0
        )

    return render(request, 'core/proyecciones.html', {
        'total_ingresos_mes': total_ingresos_mes,
        'total_gastos_mes': total_gastos_mes,
        'clientes_activos': clientes_activos,
        'top_5_proyeccion': top_5_proyeccion,
        'proyecciones_productos': proyecciones_productos,
    })


FERIADOS_2026_DEFAULT = [
    ('2026-01-01', 'Año Nuevo'),
    ('2026-01-05', 'Día de los Santos Reyes'),
    ('2026-01-21', 'Día de la Altagracia'),
    ('2026-01-26', 'Natalicio de Juan Pablo Duarte'),
    ('2026-02-27', 'Día de la Independencia'),
    ('2026-04-03', 'Viernes Santo'),
    ('2026-05-04', 'Día del Trabajo'),
    ('2026-06-04', 'Corpus Christi'),
    ('2026-08-16', 'Día de la Restauración'),
    ('2026-09-24', 'Día de las Mercedes'),
    ('2026-11-09', 'Día de la Constitución'),
    ('2026-12-25', 'Día de Navidad'),
]


def _calcular_rrhh_empleado(
    empleado, cantidad_ausencias_mes=0, *, datos_empresa=None, tope_tss=None, tramos=None,
):
    """Resumen administrativo usando el motor configurable de nómina.

    Los parámetros fiscales opcionales se reutilizan para todos los empleados
    de una misma pantalla. Esto evita consultas repetidas a Neon sin cambiar
    ningún cálculo ni dato mostrado.
    """
    salario = Decimal(empleado.salario)
    hoy = now().date()
    datos_empresa = datos_empresa or DatosEmpresa.obtener()
    tope_tss = tope_tss or nomina_servicio.obtener_tope_tss(hoy.year)
    tramos = tramos if tramos is not None else nomina_servicio.obtener_tramos_isr(hoy.year)
    pago_horas_extra = nomina_servicio.calcular_pago_horas_extra(
        salario, empleado.horas_extra_mes or Decimal('0'), datos_empresa=datos_empresa
    )
    isr_mensual, afp, sfs = nomina_servicio.calcular_isr_mensual(
        salario, pago_horas_extra, hoy.year,
        datos_empresa=datos_empresa, tope_tss=tope_tss, tramos=tramos,
    )
    descuento_ausencias = nomina_servicio.calcular_descuento_ausencias(
        salario, cantidad_ausencias_mes, datos_empresa=datos_empresa
    )
    vacaciones = nomina_servicio.calcular_vacaciones(empleado, hoy)
    salario_navidad = nomina_servicio.calcular_regalia(empleado, hoy.year)
    salario_neto = salario + pago_horas_extra - afp - sfs - isr_mensual - descuento_ausencias
    return {
        'empleado': empleado,
        'salario_bruto': salario,
        'afp': afp,
        'sfs': sfs,
        'isr': isr_mensual,
        'cantidad_ausencias_mes': cantidad_ausencias_mes,
        'descuento_ausencias': descuento_ausencias,
        'salario_neto': salario_neto,
        'horas_extra_horas': empleado.horas_extra_mes,
        'pago_horas_extra': pago_horas_extra,
        'salario_navidad': salario_navidad,
        'dias_vacaciones_correspondientes': vacaciones['dias_correspondientes'],
        'dias_vacaciones_disponibles': vacaciones['dias_restantes'],
    }



def _generar_pdf_rrhh(detalle, totales):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    estilos = getSampleStyleSheet()
    elementos = [
        Paragraph('Cromf Finanzas - Reporte de RRHH', estilos['Title']),
        Paragraph(f"Generado el {now().strftime('%d-%m-%Y %H:%M')}", estilos['Normal']),
        Spacer(1, 0.5 * cm),
    ]

    tabla_kpi = Table([
        ['Total AFP', f"RD$ {totales['afp']:,.2f}"],
        ['Total SFS', f"RD$ {totales['sfs']:,.2f}"],
        ['Total ISR', f"RD$ {totales['isr']:,.2f}"],
        ['Descuento por Ausencias', f"RD$ {totales['ausencias']:,.2f}"],
        ['Horas Extra Estimadas', f"RD$ {totales['horas_extra']:,.2f}"],
        ['Salario Navidad Provisionado', f"RD$ {totales['navidad']:,.2f}"],
        ['Nómina Neta Base', f"RD$ {totales['neto']:,.2f}"],
    ], colWidths=[8 * cm, 6 * cm])
    tabla_kpi.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, 0), (0, -1), colors.whitesmoke),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elementos.extend([tabla_kpi, Spacer(1, 0.7 * cm)])

    # Costos patronales integrados desde rosaFinal. Son aportes de la empresa,
    # no deducciones del empleado.
    elementos.append(Paragraph('Costos Patronales (Aporte de la Empresa)', estilos['Heading2']))
    tabla_patronal = Table([
        ['INFOTEP (1% de la nómina base)', f"RD$ {totales['infotep']:,.2f}"],
        ['Riesgo Laboral SRL', f"RD$ {totales['riesgo_laboral']:,.2f}"],
        ['Total Costo Patronal', f"RD$ {totales['costo_patronal']:,.2f}"],
    ], colWidths=[8 * cm, 6 * cm])
    tabla_patronal.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, 0), (0, -1), colors.whitesmoke),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
    ]))
    elementos.extend([tabla_patronal, Spacer(1, 0.7 * cm), Paragraph('Detalle por Empleado', estilos['Heading2'])])

    filas = [['Empleado', 'Bruto', 'AFP', 'SFS', 'ISR', 'Ausencias', 'H. Extra', 'Navidad', 'Neto']]
    for item in detalle:
        filas.append([
            item['empleado'].nombre,
            f"{item['salario_bruto']:,.2f}",
            f"{item['afp']:,.2f}",
            f"{item['sfs']:,.2f}",
            f"{item['isr']:,.2f}",
            f"{item['descuento_ausencias']:,.2f}",
            f"{item['pago_horas_extra']:,.2f}",
            f"{item['salario_navidad']:,.2f}",
            f"{item['salario_neto']:,.2f}",
        ])
    if len(filas) == 1:
        filas.append(['--'] * 9)

    tabla_detalle = Table(filas, colWidths=[3.0*cm, 1.65*cm, 1.5*cm, 1.5*cm, 1.5*cm, 1.7*cm, 1.7*cm, 1.7*cm, 1.8*cm])
    tabla_detalle.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#21c99a')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 6.5),
    ]))
    elementos.append(tabla_detalle)
    doc.build(elementos)
    buffer.seek(0)
    return buffer


def _detalle_rrhh_actual(empleados_activos, datos_empresa=None):
    hoy = now().date()
    empleados_activos = list(empleados_activos)
    datos_empresa = datos_empresa or DatosEmpresa.obtener()
    tope_tss = nomina_servicio.obtener_tope_tss(hoy.year)
    tramos = nomina_servicio.obtener_tramos_isr(hoy.year)
    conteos = {
        fila['empleado_id']: fila['total']
        for fila in Ausencia.objects.filter(
            fecha__gte=hoy.replace(day=1), fecha__lte=hoy,
            empleado__in=empleados_activos, justificada=False,
        ).values('empleado_id').annotate(total=models.Count('id'))
    }
    return [
        _calcular_rrhh_empleado(
            empleado, conteos.get(empleado.id, 0),
            datos_empresa=datos_empresa, tope_tss=tope_tss, tramos=tramos,
        )
        for empleado in empleados_activos
    ]


@login_required
@requiere_permiso('empleados')
def empleados(request):
    puede_crear = puede_escribir_en(request, 'empleados')
    empleado_id = request.GET.get('editar')
    if request.method == 'POST':
        if not puede_crear:
            messages.error(request, 'No tienes permiso para modificar empleados.')
            return redirect('empleados')
        accion = request.POST.get('accion', 'crear')
        eid = request.POST.get('empleado_id')
        if accion == 'desactivar' and eid:
            # Decisión de arquitectura (RRHH): un Empleado nunca se
            # elimina físicamente para conservar su historial (Ausencia,
            # RegistroActividad, etc.). El CRUD desactiva (estado=
            # 'Inactivo') reutilizando el campo y los valores que ya
            # existían, en vez de introducir un sistema de baja lógica
            # nuevo. El empleado sigue consultable en reportes/historial;
            # solo se excluye de los cálculos que ya filtraban por
            # estado='Activo' (nómina, dashboard, etc.). Nunca se llama
            # a empleado.delete().
            empleado = get_object_or_404(Empleado, pk=eid)
            if empleado.estado == 'Inactivo':
                messages.info(request, 'Este empleado ya está inactivo.')
            else:
                empleado.estado = 'Inactivo'
                empleado.save(update_fields=['estado'])
                messages.success(request, 'Empleado desactivado correctamente. Su historial se conserva.')
            return redirect('empleados')
        empleado = get_object_or_404(Empleado, pk=eid) if accion == 'editar' and eid else None
        form = EmpleadoForm(request.POST, instance=empleado)
        if form.is_valid():
            form.save()
            messages.success(request, 'Empleado actualizado correctamente.' if empleado else 'Empleado creado correctamente.')
            return redirect('empleados')
    else:
        form = EmpleadoForm(instance=get_object_or_404(Empleado, pk=empleado_id)) if empleado_id else EmpleadoForm()
    # Una sola consulta para el directorio. Antes este mismo QuerySet se
    # reevaluaba varias veces (count/filter/distinct), multiplicando viajes a
    # la base remota en cada cambio de página.
    lista_empleados = list(Empleado.objects.all())
    empleados_activos = [e for e in lista_empleados if e.estado == 'Activo']
    activos = len(empleados_activos)
    total_empleados = len(lista_empleados)
    departamentos = sorted({e.departamento for e in lista_empleados if e.departamento})
    nomina_mensual = sum((e.salario for e in empleados_activos), Decimal('0'))
    return render(request, 'core/empleados.html', {
        'empleados': lista_empleados, 'form': form,
        'total_empleados': total_empleados, 'activos': activos,
        'porcentaje_activos': round((activos / total_empleados) * 100) if total_empleados else 0,
        'nomina_mensual': nomina_mensual,
        'nomina_empleados': empleados_activos,
        'departamentos': departamentos, 'total_departamentos': len(departamentos),
        'puede_crear': puede_crear, 'editando': bool(empleado_id),
        'tab_activa': request.GET.get('tab', 'directorio'),
    })




@login_required
@requiere_permiso('finanzas')
@require_POST
def pagar_nomina(request):
    """Compatibilidad con la antigua URL de pago individual de nómina.

    Desde esta integración, la fuente de verdad es RRHH -> Nómina, que crea
    una corrida mensual y registra un único movimiento al pagarla. Se impide
    usar el flujo antiguo para evitar pagos duplicados o inconsistentes.
    """
    messages.info(
        request,
        'El pago de nómina ahora se gestiona desde RRHH → Nómina. '
        'No se registró ningún movimiento con el flujo antiguo.'
    )
    return redirect('rrhh_nomina')


@login_required
@requiere_permiso('finanzas')
@require_POST
def registrar_cobro_venta(request, venta_id):
    """Ruta de compatibilidad usada por la pantalla Contabilidad.

    Desde esta integración el cobro real se guarda en Cobro/CuentaPorCobrar y
    genera exactamente un MovimientoFinanciero enlazado.
    """
    if not request.user.has_perm('core.registrar_cobros'):
        messages.error(request, 'No tienes permiso para registrar cobros de cuentas por cobrar.')
        return redirect(f"{reverse('contabilidad')}?cuentas_cobrar=1")

    try:
        with transaction.atomic():
            venta = Venta.objects.select_for_update().select_related('cliente').get(
                pk=venta_id, condicion_pago='Credito'
            )
            if venta.cliente is None:
                raise ValueError('La venta a crédito no tiene un cliente asociado.')
            cuenta, _ = CuentaPorCobrar.objects.get_or_create(
                venta=venta,
                defaults={
                    'cliente': venta.cliente,
                    'importe_original': venta.total,
                    'fecha_emision': venta.fecha.date(),
                    'fecha_vencimiento': venta.fecha.date() + timedelta(days=DatosEmpresa.obtener().dias_credito),
                    'creado_por': request.user,
                },
            )
            # Bloquea también la obligación antes de validar el saldo.
            cuenta = CuentaPorCobrar.objects.select_for_update().get(pk=cuenta.pk)
            saldo = cuenta.saldo_pendiente
            if saldo <= 0:
                raise ValueError(f'La venta {venta.numero_factura} ya está totalmente cobrada.')
            try:
                monto = Decimal(str(request.POST.get('monto', '')).strip())
            except Exception:
                monto = Decimal('0')
            if monto <= 0:
                raise ValueError('Indica un monto de cobro válido.')
            if monto > saldo:
                raise ValueError(f'El cobro no puede superar el saldo pendiente de RD$ {saldo:,.2f}.')

            fecha_str = request.POST.get('fecha') or now().date().isoformat()
            try:
                fecha_cobro = datetime.strptime(fecha_str, '%Y-%m-%d').date()
            except ValueError:
                raise ValueError('La fecha del cobro no es válida.')
            if fecha_cobro > now().date():
                raise ValueError('La fecha del cobro no puede estar en el futuro.')
            if fecha_cobro < venta.fecha.date():
                raise ValueError('La fecha del cobro no puede ser anterior a la venta.')

            medio_pago = request.POST.get('medio_pago') or 'Efectivo'
            medios_validos = {valor for valor, _ in MovimientoFinanciero.MEDIO_PAGO_CHOICES}
            if medio_pago not in medios_validos:
                medio_pago = 'Efectivo'
            referencia = (request.POST.get('referencia') or venta.numero_factura).strip()
            cobro = Cobro.objects.create(
                cuenta=cuenta, monto=monto, fecha=fecha_cobro,
                metodo_pago=medio_pago, referencia=referencia,
                observacion=(request.POST.get('observacion') or '').strip(),
                registrado_por=request.user,
            )
            MovimientoFinanciero.objects.create(
                tipo='Ingreso', fecha=fecha_cobro, categoria='Cobro CxC',
                cliente_proveedor=venta.cliente_nombre or venta.cliente.nombre,
                monto=monto, medio_pago=medio_pago,
                cuenta=_cuenta_automatica_por_medio(medio_pago),
                factura=venta.numero_factura, venta=venta, cobro=cobro,
            )
        messages.success(request, f'Cobro de RD$ {monto:,.2f} registrado para {venta.numero_factura}.')
    except Venta.DoesNotExist:
        messages.error(request, 'La venta a crédito indicada no existe.')
    except ValueError as exc:
        messages.error(request, str(exc))

    return redirect(f"{reverse('contabilidad')}?cuentas_cobrar=1")


@login_required
@requiere_permiso('finanzas')
def contabilidad(request):
    """Resumen contable; la nómina operativa vive en RRHH -> Nómina."""
    if request.GET.get('tab') == 'nomina':
        messages.info(request, 'La nómina ahora se gestiona desde RRHH → Nómina.')
        return redirect('rrhh_nomina')
    puede_crear = puede_escribir_en(request, 'finanzas')
    hoy = now().date()
    inicio_mes = hoy.replace(day=1)
    modal_activo = ''

    if request.method == 'POST':
        if not puede_crear:
            messages.error(request, 'No tienes permiso para registrar movimientos contables.')
            return redirect('contabilidad')

        accion = request.POST.get('accion')
        if accion == 'nuevo_ingreso':
            messages.error(
                request,
                'Los ingresos se registran automáticamente desde Ventas y Cuentas por Cobrar.'
            )
            return redirect('contabilidad')
        if accion == 'nuevo_gasto':
            datos = request.POST.copy()
            datos['tipo'] = 'Gasto'
            form_movimiento = MovimientoFinancieroForm(datos)
            modal_activo = 'gasto'

            if form_movimiento.is_valid():
                movimiento = form_movimiento.save(commit=False)
                if movimiento.monto <= 0:
                    messages.error(request, 'El monto debe ser mayor que cero.')
                elif 'nomina' in _normalizar_texto(movimiento.categoria):
                    messages.error(
                        request,
                        'Los pagos de nómina deben registrarse desde RRHH → Nómina.'
                    )
                else:
                    movimiento.save()
                    messages.success(
                        request,
                        f'{movimiento.tipo} registrado correctamente por RD$ {movimiento.monto:,.2f}.'
                    )
                    return redirect('contabilidad')
            else:
                messages.error(request, 'Revisa los datos del movimiento e inténtalo nuevamente.')
        else:
            messages.error(request, 'Acción contable no reconocida.')

    money_field = models.DecimalField(max_digits=18, decimal_places=2)
    totales_mes = MovimientoFinanciero.objects.filter(
        fecha__gte=inicio_mes, fecha__lte=hoy
    ).aggregate(
        ingresos=Coalesce(
            models.Sum('monto', filter=models.Q(tipo='Ingreso')),
            models.Value(Decimal('0'), output_field=money_field),
            output_field=money_field,
        ),
        gastos=Coalesce(
            models.Sum('monto', filter=models.Q(tipo='Gasto')),
            models.Value(Decimal('0'), output_field=money_field),
            output_field=money_field,
        ),
    )
    nomina_virtual_mes = _movimientos_nomina(inicio_mes, hoy)
    total_ingresos = totales_mes['ingresos']
    total_gastos = totales_mes['gastos'] + sum(
        (m.monto for m in nomina_virtual_mes), Decimal('0')
    )
    ganancia_perdida = total_ingresos - total_gastos

    activos_inventario = Producto.objects.aggregate(
        total=Coalesce(
            models.Sum(
                models.ExpressionWrapper(
                    models.F('costo_compra') * models.F('stock'),
                    output_field=money_field,
                )
            ),
            models.Value(Decimal('0'), output_field=money_field),
            output_field=money_field,
        )
    )['total']

    empleados_activos = list(
        Empleado.objects.filter(estado='Activo').order_by('nombre')
    )
    nomina_mensual = sum((e.salario for e in empleados_activos), Decimal('0'))
    nomina_detalle, nomina_pagada, nomina_pendiente = _estado_nomina_mes(
        empleados_activos, inicio_mes, hoy
    )

    # Compras pendientes se integran únicamente al indicador de cuentas por pagar.
    # No se registran como gasto hasta que el módulo Compras genere su pago real.
    saldo_compras_pendientes = Compra.objects.exclude(estado_pago='Pagada').aggregate(
        total=Coalesce(
            models.Sum(
                models.ExpressionWrapper(
                    models.F('total') - models.F('monto_pagado'),
                    output_field=money_field,
                )
            ),
            models.Value(Decimal('0'), output_field=money_field),
            output_field=money_field,
        )
    )['total']

    tab_activa = request.GET.get('tab', 'contabilidad')
    if tab_activa not in {'contabilidad', 'nomina'}:
        tab_activa = 'contabilidad'

    historial_nomina_activo = (
        tab_activa == 'nomina' and request.GET.get('historial_nomina') == '1'
    )
    historial_nomina = []
    if historial_nomina_activo:
        historial_nomina = list(
            MovimientoFinanciero.objects.filter(
                tipo='Gasto', categoria__icontains='mina'
            ).order_by('-fecha', '-id')
        )

    detalle_empleado = None
    detalle_pagos = []
    detalle_pagado_mes = Decimal('0')
    detalle_pendiente_mes = Decimal('0')
    detalle_id = request.GET.get('detalle_nomina') if tab_activa == 'nomina' else None
    if detalle_id:
        detalle_empleado = get_object_or_404(Empleado, pk=detalle_id)
        nombre_detalle = _normalizar_texto(detalle_empleado.nombre)
        detalle_pagos = [
            m for m in MovimientoFinanciero.objects.filter(
                tipo='Gasto', categoria__icontains='mina'
            ).order_by('-fecha', '-id')
            if _normalizar_texto(m.cliente_proveedor) == nombre_detalle
        ]
        detalle_pagado_mes, _ = _pago_empleado_en_mes(detalle_empleado, hoy)
        detalle_pagado_mes = min(detalle_pagado_mes, detalle_empleado.salario)
        detalle_pendiente_mes = max(
            detalle_empleado.salario - detalle_pagado_mes, Decimal('0')
        )

    ventas_credito_pendientes = list(_ventas_credito_con_saldo())
    cuentas_por_cobrar = sum(
        (v.saldo_pendiente_calc for v in ventas_credito_pendientes), Decimal('0')
    )
    cuentas_por_pagar = nomina_pendiente + saldo_compras_pendientes

    movimientos_mes = list(MovimientoFinanciero.objects.filter(
        fecha__gte=inicio_mes, fecha__lte=hoy
    ))
    movimientos_mes.extend(nomina_virtual_mes)
    movimientos_mes.sort(
        key=lambda m: (m.fecha, getattr(m, 'id', 0) or 0), reverse=True
    )

    ver_todos = request.GET.get('movimientos') == 'todos'
    if ver_todos:
        movimientos = list(MovimientoFinanciero.objects.all())
        movimientos.extend(nomina_virtual_mes)
        movimientos.sort(
            key=lambda m: (m.fecha, getattr(m, 'id', 0) or 0), reverse=True
        )
    else:
        movimientos = movimientos_mes[:5]

    return render(request, 'core/contabilidad.html', {
        'total_ingresos': total_ingresos,
        'total_gastos': total_gastos,
        'ganancia_perdida': ganancia_perdida,
        'activos': activos_inventario,
        'cuentas_por_cobrar': cuentas_por_cobrar,
        'ventas_credito_pendientes': ventas_credito_pendientes,
        'mostrar_cuentas_cobrar': request.GET.get('cuentas_cobrar') == '1',
        'cuentas_por_pagar': cuentas_por_pagar,
        'saldo_compras_pendientes': saldo_compras_pendientes,
        'movimientos_recientes': movimientos,
        'ver_todos': ver_todos,
        'otros_ingresos': Decimal('0'),
        'otros_gastos': Decimal('0'),
        'ganancia_bruta': ganancia_perdida,
        'ganancia_neta': ganancia_perdida,
        'mes_actual': MESES_ES.get(hoy.month, ''),
        'anio_actual': hoy.year,
        'nomina_mensual': nomina_mensual,
        'nomina_detalle': nomina_detalle,
        'nomina_pagada': nomina_pagada,
        'nomina_pendiente': nomina_pendiente,
        'nomina_pendientes': [fila for fila in nomina_detalle if fila['pendiente'] > 0],
        'empleados_activos_count': len(empleados_activos),
        'historial_nomina_activo': historial_nomina_activo,
        'historial_nomina': historial_nomina,
        'detalle_empleado': detalle_empleado,
        'detalle_pagos': detalle_pagos,
        'detalle_pagado_mes': detalle_pagado_mes,
        'detalle_pendiente_mes': detalle_pendiente_mes,
        'tab_activa': tab_activa,
        'puede_crear': puede_crear,
        'medio_pago_choices': MovimientoFinanciero.MEDIO_PAGO_CHOICES,
        'hoy': hoy,
        'modal_activo': modal_activo,
    })


# ============================================================
# REPORTES
# ============================================================

@login_required
@permission_required('core.ver_empleados', raise_exception=True)
def rrhh_resumen(request):
    empleados_activos = list(Empleado.objects.filter(estado='Activo').order_by('nombre'))
    datos_empresa = DatosEmpresa.obtener()
    detalle = _detalle_rrhh_actual(empleados_activos, datos_empresa=datos_empresa)
    nomina_base = sum((Decimal(i['salario_bruto']) for i in detalle), Decimal('0'))
    costos_patronales = nomina_servicio.calcular_costos_patronales(nomina_base, datos_empresa=datos_empresa)
    totales = {
        'afp': sum((i['afp'] for i in detalle), Decimal('0')),
        'sfs': sum((i['sfs'] for i in detalle), Decimal('0')),
        'isr': sum((i['isr'] for i in detalle), Decimal('0')),
        'ausencias': sum((i['descuento_ausencias'] for i in detalle), Decimal('0')),
        'horas_extra': sum((i['pago_horas_extra'] for i in detalle), Decimal('0')),
        'navidad': sum((i['salario_navidad'] for i in detalle), Decimal('0')),
        'neto': sum((i['salario_neto'] for i in detalle), Decimal('0')),
        'infotep': costos_patronales['infotep'],
        'riesgo_laboral': costos_patronales['riesgo_laboral'],
        'costo_patronal': costos_patronales['total_costo_patronal'],
    }
    if request.method == 'POST' and request.POST.get('accion') == 'generar_reporte_rrhh':
        if not puede_escribir_en(request, 'empleados'):
            raise PermissionDenied('No tienes permiso para generar este reporte.')
        buffer = _generar_pdf_rrhh(detalle, totales)
        response = HttpResponse(buffer.read(), content_type='application/pdf')
        response['Content-Disposition'] = 'attachment; filename="rrhh_resumen.pdf"'
        return response
    return render(request, 'core/rrhh_resumen.html', {
        'detalle_rrhh': detalle,
        'total_afp': totales['afp'],
        'total_sfs': totales['sfs'],
        'total_isr': totales['isr'],
        'total_descuento_ausencias': totales['ausencias'],
        'total_horas_extra_pago': totales['horas_extra'],
        'total_navidad': totales['navidad'],
        'total_neto': totales['neto'],
        'infotep': totales['infotep'],
        'riesgo_laboral': totales['riesgo_laboral'],
        'total_costo_patronal': totales['costo_patronal'],
        'empresa': datos_empresa,
        'puede_crear': puede_escribir_en(request, 'empleados'),
    })


@login_required
@permission_required('core.ver_ausencias', raise_exception=True)
def rrhh_ausencias(request):
    puede_gestionar = request.user.has_perm('core.gestionar_ausencias')
    ausencia_id = request.GET.get('editar')

    if request.method == 'POST':
        if not puede_gestionar:
            raise PermissionDenied('No tienes permiso para gestionar ausencias.')
        accion = request.POST.get('accion', 'crear')
        aid = request.POST.get('ausencia_id')
        if accion == 'eliminar' and aid:
            ausencia = get_object_or_404(Ausencia, pk=aid)
            ausencia.delete()
            messages.success(request, 'Registro de ausencia eliminado.')
            return redirect('rrhh_ausencias')
        ausencia = get_object_or_404(Ausencia, pk=aid) if accion == 'editar' and aid else None
        form = AusenciaForm(request.POST, instance=ausencia)
        if form.is_valid():
            form.save()
            messages.success(request, 'Ausencia actualizada correctamente.' if ausencia else 'Ausencia registrada correctamente.')
            return redirect('rrhh_ausencias')
    else:
        form = AusenciaForm(instance=get_object_or_404(Ausencia, pk=ausencia_id)) if ausencia_id else AusenciaForm()

    return render(request, 'core/rrhh_ausencias.html', {
        'ausencias': Ausencia.objects.select_related('empleado').all(),
        'form': form,
        'puede_gestionar': puede_gestionar,
        'editando': bool(ausencia_id),
    })

@login_required
@permission_required('core.ver_feriados', raise_exception=True)
def rrhh_feriados(request):
    puede_gestionar = request.user.has_perm('core.gestionar_feriados')
    feriado_id = request.GET.get('editar')

    if request.method == 'POST':
        if not puede_gestionar:
            raise PermissionDenied('No tienes permiso para gestionar feriados.')
        accion = request.POST.get('accion', 'crear')
        fid = request.POST.get('feriado_id')
        if accion == 'eliminar' and fid:
            feriado = get_object_or_404(DiaFeriado, pk=fid)
            feriado.delete()
            messages.success(request, 'Día feriado eliminado.')
            return redirect('rrhh_feriados')
        feriado = get_object_or_404(DiaFeriado, pk=fid) if accion == 'editar' and fid else None
        form = DiaFeriadoForm(request.POST, instance=feriado)
        if form.is_valid():
            form.save()
            messages.success(request, 'Feriado actualizado correctamente.' if feriado else 'Feriado creado correctamente.')
            return redirect('rrhh_feriados')
    else:
        form = DiaFeriadoForm(instance=get_object_or_404(DiaFeriado, pk=feriado_id)) if feriado_id else DiaFeriadoForm()

    return render(request, 'core/rrhh_feriados.html', {
        'feriados': DiaFeriado.objects.all(),
        'form': form,
        'puede_gestionar': puede_gestionar,
        'editando': bool(feriado_id),
    })

@login_required
@permission_required('core.ver_nomina', raise_exception=True)
def rrhh_nomina(request):
    puede_gestionar = request.user.has_perm('core.gestionar_nomina')

    if request.method == 'POST':
        if not puede_gestionar:
            raise PermissionDenied('No tienes permiso para generar nómina.')
        try:
            anio = int(request.POST.get('anio'))
            mes = int(request.POST.get('mes'))
            nomina_servicio.generar_nomina_mensual(anio, mes, generado_por=request.user)
            messages.success(request, f'Nómina de {mes:02d}/{anio} generada correctamente.')
        except ValueError as e:
            messages.error(request, str(e))
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
        return redirect('rrhh_nomina')

    return render(request, 'core/rrhh_nomina.html', {
        'puede_gestionar': puede_gestionar,
        'nominas': Nomina.objects.all()[:24],
        'anio_actual': now().year,
        'mes_actual': now().month,
    })

@login_required
@permission_required('core.ver_nomina', raise_exception=True)
def rrhh_nomina_detalle(request, nomina_id):
    nomina = get_object_or_404(Nomina, pk=nomina_id)
    return render(request, 'core/rrhh_nomina_detalle.html', {
        'nomina': nomina,
        'detalles': nomina.detalles.select_related('empleado').all(),
        'puede_pagar': request.user.has_perm('core.registrar_pago_nomina'),
    })

@login_required
@permission_required('core.registrar_pago_nomina', raise_exception=True)
@require_POST
def rrhh_nomina_pagar(request, nomina_id):
    """Fase 3 (Nómina - Registrar pago). Espejo exacto de
    compras_registrar_pago / cuenta_por_cobrar_registrar_cobro:
    select_for_update() bloquea la fila de Nomina ANTES de leer su
    estado, dentro de una única transaction.atomic() que también crea
    el MovimientoFinanciero y marca la Nomina como Pagada. Así dos
    solicitudes de pago simultáneas sobre la misma Nomina nunca pueden
    generar dos egresos: la segunda espera a que la primera confirme,
    y al leer el estado ya actualizado ('Pagada') se rechaza.

    No se recalcula NADA de la nómina aquí (ni AFP, SFS, ISR, horas
    extra, ausencias, vacaciones ni regalía): se usa exclusivamente el
    total_neto ya persistido por generar_nomina_mensual(). Este pago
    es una operación puramente financiera, deliberadamente separada
    del cálculo de nómina (ver servicios_nomina.py).
    """
    try:
        with transaction.atomic():
            nomina = Nomina.objects.select_for_update().get(pk=nomina_id)
            if nomina.estado == 'Pagada':
                raise ValueError('Esta nómina ya fue pagada. No se puede registrar el pago nuevamente.')

            fecha_pago = now().date()
            periodo = nomina.fecha_inicio.strftime('%m/%Y')

            # Un solo MovimientoFinanciero por TODA la nómina (no uno
            # por empleado): la distribución individual ya queda
            # representada en DetalleNomina. Relación OneToOne
            # (MovimientoFinanciero.nomina), mismo criterio exacto que
            # 'cobro' y 'pago_compra' - la base de datos impide que
            # esta misma Nomina termine asociada a dos movimientos.
            MovimientoFinanciero.objects.create(
                tipo='Gasto', fecha=fecha_pago, categoria='Nómina',
                cliente_proveedor='Nómina de empleados',
                monto=nomina.total_neto, medio_pago='Transferencia',
                cuenta=_cuenta_automatica_por_medio('Transferencia'),
                factura=f'NOM-{nomina.pk:06d}',
                nomina=nomina,
            )

            nomina.estado = 'Pagada'
            nomina.fecha_pago = fecha_pago
            nomina.save(update_fields=['estado', 'fecha_pago'])
    except Nomina.DoesNotExist:
        messages.error(request, 'La nómina no existe.')
        return redirect('rrhh_nomina')
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('rrhh_nomina_detalle', nomina_id=nomina_id)

    messages.success(
        request,
        f'Pago de la nómina de {periodo} registrado por RD$ {nomina.total_neto}.',
    )
    return redirect('rrhh_nomina_detalle', nomina_id=nomina_id)

@login_required
@permission_required('core.ver_nomina', raise_exception=True)
def rrhh_nomina_pdf(request, nomina_id):
    """Fase 4 (Nómina - PDF completo). Descarga la nómina completa en
    PDF. Mismo permiso que verla en pantalla (ver_nomina) - igual
    criterio que venta_factura/venta_factura_pdf, donde ver y
    descargar comparten el mismo permiso de lectura del módulo.

    Solo lee Nomina/DetalleNomina ya persistidos; no recalcula nada."""
    nomina = get_object_or_404(Nomina, pk=nomina_id)
    detalles = nomina.detalles.select_related('empleado').all()
    buffer = _generar_pdf_nomina(nomina, detalles, DatosEmpresa.obtener())
    response = HttpResponse(buffer.read(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="nomina-{nomina.fecha_inicio.strftime("%Y-%m")}.pdf"'
    return response

def _generar_pdf_nomina(nomina, detalles, empresa):
    """Genera el PDF de una Nomina completa con la misma información
    que ya se muestra en rrhh_nomina_detalle.html (mismas columnas,
    mismo origen de datos: Nomina/DetalleNomina ya persistidos). Nunca
    recalcula AFP/SFS/ISR/horas extra/ausencias/neto - solo lee los
    valores ya guardados, igual que _generar_pdf_factura hace con
    Venta/DetalleVenta."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        topMargin=1.8 * cm, bottomMargin=1.8 * cm, leftMargin=1.4 * cm, rightMargin=1.4 * cm,
    )
    estilos = getSampleStyleSheet()
    COLOR_OSCURO = colors.HexColor('#0f1524')
    COLOR_VERDE = colors.HexColor('#21c99a')
    COLOR_GRIS = colors.HexColor('#6B7280')

    estilo_empresa_nombre = ParagraphStyle('EmpresaNombreNom', parent=estilos['Normal'], fontSize=15, fontName='Helvetica-Bold', textColor=COLOR_OSCURO, spaceAfter=4)
    estilo_gris = ParagraphStyle('GrisNom', parent=estilos['Normal'], fontSize=9, textColor=COLOR_GRIS, leading=13)
    estilo_titulo = ParagraphStyle('TituloNom', parent=estilos['Normal'], fontSize=20, fontName='Helvetica-Bold', textColor=COLOR_VERDE, alignment=2, spaceAfter=4)
    estilo_periodo = ParagraphStyle('PeriodoNom', parent=estilos['Normal'], fontSize=10.5, fontName='Helvetica-Bold', textColor=COLOR_OSCURO, alignment=2)
    estilo_meta = ParagraphStyle('MetaNom', parent=estilos['Normal'], fontSize=9, textColor=COLOR_GRIS, alignment=2)

    elementos = []

    # ---- Encabezado: empresa (izq.) + NÓMINA/período/estado (der.) ----
    datos_empresa = [Paragraph(empresa.nombre_comercial, estilo_empresa_nombre)]
    lineas_empresa = []
    if empresa.rnc:
        lineas_empresa.append(f"RNC: {empresa.rnc}")
    if empresa.direccion:
        lineas_empresa.append(empresa.direccion)
    if lineas_empresa:
        datos_empresa.append(Paragraph('<br/>'.join(lineas_empresa), estilo_gris))

    datos_documento = [
        Paragraph('NÓMINA', estilo_titulo),
        Paragraph(f"Período: {nomina.fecha_inicio.strftime('%m/%Y')}", estilo_periodo),
        Paragraph(
            f"{nomina.fecha_inicio.strftime('%d/%m/%Y')} — {nomina.fecha_fin.strftime('%d/%m/%Y')}",
            estilo_meta,
        ),
        Paragraph(f"Generada: {nomina.fecha_generacion.strftime('%d/%m/%Y %H:%M')}", estilo_meta),
        Paragraph(f"Estado: {nomina.estado}", estilo_meta),
    ]
    if nomina.estado == 'Pagada' and nomina.fecha_pago:
        datos_documento.append(Paragraph(f"Pagada: {nomina.fecha_pago.strftime('%d/%m/%Y')}", estilo_meta))

    tabla_encabezado = Table([[datos_empresa, datos_documento]], colWidths=[11 * cm, 8 * cm])
    tabla_encabezado.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LINEBELOW', (0, 0), (-1, 0), 1.5, COLOR_OSCURO),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
    ]))
    elementos.append(tabla_encabezado)
    elementos.append(Spacer(1, 0.5 * cm))

    # ---- Detalle por empleado: valores históricos de DetalleNomina ----
    filas = [['Empleado', 'Salario', 'H. extra', 'Ausencias', 'AFP', 'SFS', 'ISR', 'Deducciones', 'Neto']]
    for d in detalles:
        filas.append([
            f"{d.empleado_nombre} ({d.tipo_nomina})",
            f"{d.salario_base:,.2f}",
            f"{d.pago_horas_extra:,.2f}",
            f"{d.descuento_ausencias:,.2f}",
            f"{d.afp:,.2f}",
            f"{d.sfs:,.2f}",
            f"{d.isr:,.2f}",
            f"{d.total_deducciones:,.2f}",
            f"{d.salario_neto:,.2f}",
        ])
    tabla_detalle = Table(
        filas,
        colWidths=[3.6 * cm, 2.1 * cm, 1.9 * cm, 2.0 * cm, 1.8 * cm, 1.8 * cm, 1.8 * cm, 2.3 * cm, 2.1 * cm],
        repeatRows=1,
    )
    tabla_detalle.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), COLOR_OSCURO),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 7.5),
        ('LINEBELOW', (0, 1), (-1, -1), 0.5, colors.HexColor('#E5E7EB')),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
    ]))
    elementos.append(tabla_detalle)
    elementos.append(Spacer(1, 0.6 * cm))

    # ---- Totales generales: leídos de Nomina, nunca sumados aquí ----
    filas_totales = [
        ['Total bruto', f"RD$ {nomina.total_bruto:,.2f}"],
        ['Total deducciones', f"RD$ {nomina.total_deducciones:,.2f}"],
        ['TOTAL NETO', f"RD$ {nomina.total_neto:,.2f}"],
    ]
    tabla_totales = Table(filas_totales, colWidths=[14 * cm, 4.7 * cm])
    tabla_totales.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -2), 9.5),
        ('FONTSIZE', (0, -1), (-1, -1), 13),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('TEXTCOLOR', (0, -1), (-1, -1), COLOR_OSCURO),
        ('TEXTCOLOR', (0, 0), (-1, -2), colors.HexColor('#374151')),
        ('LINEABOVE', (0, -1), (-1, -1), 1.2, COLOR_OSCURO),
        ('TOPPADDING', (0, -1), (-1, -1), 8),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
    ]))
    elementos.append(tabla_totales)

    doc.build(elementos)
    buffer.seek(0)
    return buffer

@login_required
@permission_required('core.ver_nomina', raise_exception=True)
def rrhh_nomina_recibo(request, detalle_id):
    """Fase 4 (Nómina - Recibo individual). Preview en pantalla del
    recibo de UN DetalleNomina ya generado. Igual que
    venta_factura/venta_factura_pdf: esto es la vista 'ver', el PDF es
    la vista 'descargar', ambas leen exactamente los mismos datos ya
    persistidos (nunca Empleado.salario actual)."""
    detalle = get_object_or_404(DetalleNomina.objects.select_related('nomina', 'empleado'), pk=detalle_id)
    return render(request, 'core/rrhh_nomina_recibo.html', {
        'detalle': detalle,
        'nomina': detalle.nomina,
        'empresa': DatosEmpresa.obtener(),
    })

@login_required
@permission_required('core.ver_nomina', raise_exception=True)
def rrhh_nomina_recibo_pdf(request, detalle_id):
    detalle = get_object_or_404(DetalleNomina.objects.select_related('nomina', 'empleado'), pk=detalle_id)
    buffer = _generar_pdf_recibo_nomina(detalle, DatosEmpresa.obtener())
    response = HttpResponse(buffer.read(), content_type='application/pdf')
    nombre_archivo = f"recibo-{detalle.empleado_nombre}-{detalle.nomina.fecha_inicio.strftime('%Y-%m')}.pdf".replace(' ', '-')
    response['Content-Disposition'] = f'attachment; filename="{nombre_archivo}"'
    return response

def _generar_pdf_recibo_nomina(detalle, empresa):
    """Genera el PDF del recibo individual de UN DetalleNomina.
    Regla crítica (Fase 4): todos los valores vienen exclusivamente
    de DetalleNomina (snapshot histórico) - nunca de Empleado.salario
    actual ni de un nuevo cálculo. Si el salario del empleado cambió
    después de generar esta nómina, este recibo sigue mostrando lo que
    realmente se pagó en su momento."""
    nomina = detalle.nomina
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        topMargin=1.8 * cm, bottomMargin=1.8 * cm, leftMargin=1.8 * cm, rightMargin=1.8 * cm,
    )
    estilos = getSampleStyleSheet()
    COLOR_OSCURO = colors.HexColor('#0f1524')
    COLOR_VERDE = colors.HexColor('#21c99a')
    COLOR_GRIS = colors.HexColor('#6B7280')

    estilo_empresa_nombre = ParagraphStyle('EmpresaNombreRec', parent=estilos['Normal'], fontSize=15, fontName='Helvetica-Bold', textColor=COLOR_OSCURO, spaceAfter=4)
    estilo_gris = ParagraphStyle('GrisRec', parent=estilos['Normal'], fontSize=9, textColor=COLOR_GRIS, leading=13)
    estilo_titulo = ParagraphStyle('TituloRec', parent=estilos['Normal'], fontSize=18, fontName='Helvetica-Bold', textColor=COLOR_VERDE, alignment=2, spaceAfter=4)
    estilo_meta = ParagraphStyle('MetaRec', parent=estilos['Normal'], fontSize=9, textColor=COLOR_GRIS, alignment=2)
    estilo_seccion = ParagraphStyle('SeccionRec', parent=estilos['Normal'], fontSize=8.5, textColor=COLOR_GRIS, spaceAfter=2)
    estilo_empleado_nombre = ParagraphStyle('EmpleadoNombreRec', parent=estilos['Normal'], fontSize=13, fontName='Helvetica-Bold', textColor=COLOR_OSCURO, spaceAfter=2)

    elementos = []

    datos_empresa = [Paragraph(empresa.nombre_comercial, estilo_empresa_nombre)]
    lineas_empresa = []
    if empresa.rnc:
        lineas_empresa.append(f"RNC: {empresa.rnc}")
    if empresa.direccion:
        lineas_empresa.append(empresa.direccion)
    if lineas_empresa:
        datos_empresa.append(Paragraph('<br/>'.join(lineas_empresa), estilo_gris))

    datos_documento = [
        Paragraph('RECIBO DE NÓMINA', estilo_titulo),
        Paragraph(f"Período: {nomina.fecha_inicio.strftime('%m/%Y')}", estilo_meta),
        Paragraph(f"Estado de la nómina: {nomina.estado}", estilo_meta),
    ]

    tabla_encabezado = Table([[datos_empresa, datos_documento]], colWidths=[10.5 * cm, 6.5 * cm])
    tabla_encabezado.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LINEBELOW', (0, 0), (-1, 0), 1.5, COLOR_OSCURO),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
    ]))
    elementos.append(tabla_encabezado)
    elementos.append(Spacer(1, 0.5 * cm))

    elementos.append(Paragraph('EMPLEADO', estilo_seccion))
    elementos.append(Paragraph(detalle.empleado_nombre, estilo_empleado_nombre))
    elementos.append(Spacer(1, 0.5 * cm))

    filas = [
        ['Tipo de nómina', detalle.tipo_nomina],
    ]
    if detalle.tipo_nomina == 'Quincenal':
        filas.extend([
            ['1ra quincena', f"RD$ {detalle.primera_quincena:,.2f}"],
            ['2da quincena', f"RD$ {detalle.segunda_quincena:,.2f}"],
        ])
    filas.extend([
        ['Salario base', f"RD$ {detalle.salario_base:,.2f}"],
        ['Horas extra', f"{detalle.horas_extra}"],
        ['Pago horas extra', f"RD$ {detalle.pago_horas_extra:,.2f}"],
        ['Días de ausencia', f"{detalle.dias_ausencia}"],
        ['Descuento por ausencias', f"-RD$ {detalle.descuento_ausencias:,.2f}"],
        ['AFP', f"-RD$ {detalle.afp:,.2f}"],
        ['SFS', f"-RD$ {detalle.sfs:,.2f}"],
        ['ISR', f"-RD$ {detalle.isr:,.2f}"],
        ['Salario bruto', f"RD$ {detalle.salario_bruto:,.2f}"],
        ['Total deducciones', f"-RD$ {detalle.total_deducciones:,.2f}"],
        ['SALARIO NETO', f"RD$ {detalle.salario_neto:,.2f}"],
    ])
    tabla = Table(filas, colWidths=[10 * cm, 4 * cm])
    tabla.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -2), 9.5),
        ('FONTSIZE', (0, -1), (-1, -1), 13),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('TEXTCOLOR', (0, -1), (-1, -1), COLOR_OSCURO),
        ('TEXTCOLOR', (0, 0), (-1, -2), colors.HexColor('#374151')),
        ('LINEABOVE', (0, -1), (-1, -1), 1.2, COLOR_OSCURO),
        ('LINEABOVE', (0, -3), (-1, -3), 0.5, colors.HexColor('#E5E7EB')),
        ('TOPPADDING', (0, -1), (-1, -1), 8),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
    ]))
    elementos.append(tabla)

    doc.build(elementos)
    buffer.seek(0)
    return buffer

MESES_ES = {
    1: 'Enero', 2: 'Febrero', 3: 'Marzo', 4: 'Abril', 5: 'Mayo', 6: 'Junio',
    7: 'Julio', 8: 'Agosto', 9: 'Septiembre', 10: 'Octubre', 11: 'Noviembre', 12: 'Diciembre',
}


def _rango_por_defecto():
    hoy = now().date()
    desde = hoy.replace(day=1)
    return desde, hoy


def _tendencia(actual, anterior):
    if anterior:
        pct = ((actual - anterior) / anterior) * 100
        return abs(round(pct, 1)), pct >= 0
    return None, True


def _totales_financieros(desde, hasta):
    money_field = models.DecimalField(max_digits=16, decimal_places=2)
    totales = MovimientoFinanciero.objects.filter(
        fecha__gte=desde, fecha__lte=hasta
    ).aggregate(
        ingresos=Coalesce(
            models.Sum('monto', filter=models.Q(tipo='Ingreso')),
            models.Value(Decimal('0'), output_field=money_field),
            output_field=money_field,
        ),
        gastos=Coalesce(
            models.Sum('monto', filter=models.Q(tipo='Gasto')),
            models.Value(Decimal('0'), output_field=money_field),
            output_field=money_field,
        ),
    )
    return totales['ingresos'], totales['gastos'] + _nomina_periodo(desde, hasta)

def _grafica_financiero_ultimos_meses(hasta, cantidad=4):
    meses = []
    cursor = hasta.replace(day=1)
    for _ in range(cantidad):
        meses.append(cursor)
        if cursor.month == 1:
            cursor = cursor.replace(year=cursor.year - 1, month=12)
        else:
            cursor = cursor.replace(month=cursor.month - 1)
    meses.reverse()

    primer_mes = meses[0]
    money_field = models.DecimalField(max_digits=16, decimal_places=2)
    filas = (
        MovimientoFinanciero.objects
        .filter(fecha__gte=primer_mes, fecha__lte=hasta)
        .annotate(mes=TruncMonth('fecha'))
        .values('mes')
        .annotate(
            ingresos=Coalesce(
                models.Sum('monto', filter=models.Q(tipo='Ingreso')),
                models.Value(Decimal('0'), output_field=money_field),
                output_field=money_field,
            ),
            gastos=Coalesce(
                models.Sum('monto', filter=models.Q(tipo='Gasto')),
                models.Value(Decimal('0'), output_field=money_field),
                output_field=money_field,
            ),
        )
    )
    reales = {
        (fila['mes'].year, fila['mes'].month): (fila['ingresos'], fila['gastos'])
        for fila in filas
    }
    nomina_virtual = defaultdict(lambda: Decimal('0'))
    for mov in _movimientos_nomina(primer_mes, hasta):
        nomina_virtual[(mov.fecha.year, mov.fecha.month)] += mov.monto

    datos = []
    for mes in meses:
        ingresos, gastos_reales = reales.get((mes.year, mes.month), (Decimal('0'), Decimal('0')))
        gastos = gastos_reales + nomina_virtual[(mes.year, mes.month)]
        datos.append({'mes': MESES_ES[mes.month], 'ingresos': ingresos, 'gastos': gastos})

    valor_max_real = max([d['ingresos'] for d in datos] + [d['gastos'] for d in datos] + [Decimal('0')])
    valor_max = valor_max_real or Decimal('1')
    for d in datos:
        d['altura_ingreso'] = int((d['ingresos'] / valor_max) * 150) if valor_max else 0
        d['altura_gasto'] = int((d['gastos'] / valor_max) * 150) if valor_max else 0

    pasos = 4
    eje_y = [(valor_max_real * i / pasos) for i in range(pasos, -1, -1)] if valor_max_real else []
    return datos, eje_y

def _generar_pdf_financiero(desde, hasta, total_ingresos, total_gastos, movimientos):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    estilos = getSampleStyleSheet()
    elementos = []

    elementos.append(Paragraph("Cromf Finanzas - Reporte Financiero", estilos['Title']))
    elementos.append(Paragraph(
        f"Periodo: {desde.strftime('%d-%m-%Y')} al {hasta.strftime('%d-%m-%Y')}", estilos['Normal']
    ))
    elementos.append(Spacer(1, 0.5 * cm))

    utilidad = total_ingresos - total_gastos
    tabla_kpi = Table([
        ['Ingresos Totales', f"RD$ {total_ingresos:,.2f}"],
        ['Gastos Totales', f"RD$ {total_gastos:,.2f}"],
        ['Utilidad Neta', f"RD$ {utilidad:,.2f}"],
    ], colWidths=[8 * cm, 6 * cm])
    tabla_kpi.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, 0), (0, -1), colors.whitesmoke),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elementos.append(tabla_kpi)
    elementos.append(Spacer(1, 1 * cm))

    elementos.append(Paragraph("Detalle de Movimientos", estilos['Heading2']))
    filas = [['Fecha', 'Tipo', 'Categoria', 'Cliente/Proveedor', 'Monto']]
    for m in movimientos:
        filas.append([
            m.fecha.strftime('%d-%m-%Y'), m.tipo, m.categoria,
            m.cliente_proveedor or '--', f"RD$ {m.monto:,.2f}",
        ])
    if len(filas) == 1:
        filas.append(['--', '--', 'Sin movimientos en el periodo', '--', '--'])

    tabla_mov = Table(filas, colWidths=[2.5 * cm, 2.2 * cm, 4 * cm, 4 * cm, 3 * cm])
    tabla_mov.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#21c99a')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
    ]))
    elementos.append(tabla_mov)

    doc.build(elementos)
    buffer.seek(0)
    return buffer


def _totales_ventas(desde, hasta):
    inicio_dt, fin_dt = _rango_datetime(desde, hasta)
    resultado = Venta.objects.filter(fecha__gte=inicio_dt, fecha__lt=fin_dt).aggregate(
        total=models.Sum('total'), cantidad=models.Count('id')
    )
    return resultado['total'] or Decimal('0'), resultado['cantidad'] or 0

def _grafica_ventas_ultimos_meses(hasta, cantidad=4):
    meses = []
    cursor = hasta.replace(day=1)
    for _ in range(cantidad):
        meses.append(cursor)
        if cursor.month == 1:
            cursor = cursor.replace(year=cursor.year - 1, month=12)
        else:
            cursor = cursor.replace(month=cursor.month - 1)
    meses.reverse()

    inicio_dt, fin_dt = _rango_datetime(meses[0], hasta)
    filas = (
        Venta.objects.filter(fecha__gte=inicio_dt, fecha__lt=fin_dt)
        .annotate(mes=TruncMonth('fecha'))
        .values('mes')
        .annotate(total=models.Sum('total'))
    )
    por_mes = {(f['mes'].year, f['mes'].month): (f['total'] or Decimal('0')) for f in filas}

    datos = [
        {
            'mes': MESES_ES[mes.month],
            'total': por_mes.get((mes.year, mes.month), Decimal('0')),
            'ventas': por_mes.get((mes.year, mes.month), Decimal('0')),
        }
        for mes in meses
    ]
    valor_max = max([d['total'] for d in datos] + [Decimal('1')])
    for d in datos:
        d['altura'] = int((d['total'] / valor_max) * 150) if valor_max else 0
        d['altura_venta'] = d['altura']
    return datos

_PALETA_CATEGORIAS = ['#10B981', '#1E293B', '#64748B', '#22C55E', '#F59E0B', '#3B82F6', '#EF4444']


def _grafica_ventas_por_categoria(hasta, cantidad=4):
    """Ventas reales por categoría agrupadas en PostgreSQL."""
    orden_choices = [codigo for codigo, _ in Producto.CATEGORIA_CHOICES]
    categorias_en_uso = set(Producto.objects.values_list('categoria', flat=True).distinct())
    categorias = [c for c in orden_choices if c in categorias_en_uso]
    categorias += sorted(categorias_en_uso - set(categorias))

    meses = []
    cursor = hasta.replace(day=1)
    for _ in range(cantidad):
        meses.append(cursor)
        if cursor.month == 1:
            cursor = cursor.replace(year=cursor.year - 1, month=12)
        else:
            cursor = cursor.replace(month=cursor.month - 1)
    meses.reverse()

    totales_por_mes = {mes: defaultdict(lambda: Decimal('0')) for mes in meses}
    if categorias:
        inicio_dt, fin_dt = _rango_datetime(meses[0], hasta)
        money_field = models.DecimalField(max_digits=18, decimal_places=2)
        filas = (
            DetalleVenta.objects
            .filter(venta__fecha__gte=inicio_dt, venta__fecha__lt=fin_dt)
            .annotate(mes=TruncMonth('venta__fecha'))
            .values('mes', 'producto__categoria')
            .annotate(
                monto=Coalesce(
                    models.Sum(
                        models.ExpressionWrapper(
                            models.F('cantidad') * models.F('precio_unitario'),
                            output_field=money_field,
                        )
                    ),
                    models.Value(Decimal('0'), output_field=money_field),
                    output_field=money_field,
                )
            )
        )
        for fila in filas:
            mes_fecha = fila['mes'].date() if hasattr(fila['mes'], 'date') else fila['mes']
            clave_mes = mes_fecha.replace(day=1)
            if clave_mes in totales_por_mes:
                totales_por_mes[clave_mes][fila['producto__categoria']] = fila['monto']

    datos = []
    for mes in meses:
        montos = {cat: totales_por_mes[mes].get(cat, Decimal('0')) for cat in categorias}
        total_mes = sum(montos.values(), Decimal('0'))
        datos.append({'mes': MESES_ES[mes.month], 'total': total_mes, 'montos': montos})

    valor_max_mes = max([d['total'] for d in datos] + [Decimal('0')])
    for d in datos:
        d['altura_pct'] = float((d['total'] / valor_max_mes) * 100) if valor_max_mes else 0
        d['segmentos'] = [
            {
                'categoria': cat,
                'monto': d['montos'][cat],
                'pct_del_total': float((d['montos'][cat] / d['total']) * 100) if d['total'] else 0,
                'color': _PALETA_CATEGORIAS[i % len(_PALETA_CATEGORIAS)],
            }
            for i, cat in enumerate(categorias)
        ]

    eje_y = [(valor_max_mes * i / 5) for i in range(5, -1, -1)] if valor_max_mes else []
    leyenda = [
        {'categoria': cat, 'color': _PALETA_CATEGORIAS[i % len(_PALETA_CATEGORIAS)]}
        for i, cat in enumerate(categorias)
    ]
    return {
        'leyenda': leyenda,
        'datos': datos,
        'eje_y': eje_y,
        'hay_datos': bool(categorias) and valor_max_mes > 0,
        'hay_categorias': bool(categorias),
    }

def _top_productos_vendidos(desde, hasta, top_n=5):
    inicio_dt, fin_dt = _rango_datetime(desde, hasta)
    money_field = models.DecimalField(max_digits=18, decimal_places=2)
    filas = (
        DetalleVenta.objects
        .filter(venta__fecha__gte=inicio_dt, venta__fecha__lt=fin_dt)
        .values('producto_id', 'producto__sku', 'producto__nombre', 'producto__categoria')
        .annotate(
            unidades=models.Sum('cantidad'),
            monto=models.Sum(
                models.ExpressionWrapper(
                    models.F('cantidad') * models.F('precio_unitario'),
                    output_field=money_field,
                )
            ),
        )
        .order_by('-monto')[:top_n]
    )
    return [
        {
            'producto': SimpleNamespace(
                id=f['producto_id'], sku=f['producto__sku'],
                nombre=f['producto__nombre'], categoria=f['producto__categoria'],
            ),
            'unidades': f['unidades'] or 0,
            'monto': f['monto'] or Decimal('0'),
        }
        for f in filas
    ]

def _generar_pdf_ventas(desde, hasta, total_vendido, cantidad_ventas, ticket_promedio, top_productos):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    estilos = getSampleStyleSheet()
    elementos = []

    elementos.append(Paragraph("Cromf Finanzas - Reporte de Ventas", estilos['Title']))
    elementos.append(Paragraph(
        f"Periodo: {desde.strftime('%d-%m-%Y')} al {hasta.strftime('%d-%m-%Y')}", estilos['Normal']
    ))
    elementos.append(Spacer(1, 0.5 * cm))

    tabla_kpi = Table([
        ['Total Vendido', f"RD$ {total_vendido:,.2f}"],
        ['Cantidad de Ventas', str(cantidad_ventas)],
        ['Ticket Promedio', f"RD$ {ticket_promedio:,.2f}"],
    ], colWidths=[8 * cm, 6 * cm])
    tabla_kpi.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, 0), (0, -1), colors.whitesmoke),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elementos.append(tabla_kpi)
    elementos.append(Spacer(1, 1 * cm))

    elementos.append(Paragraph("Top Productos Vendidos", estilos['Heading2']))
    filas = [['Producto', 'Categoria', 'Unidades', 'Monto Total']]
    for item in top_productos:
        filas.append([
            item['producto'].nombre, item['producto'].categoria,
            str(item['unidades']), f"RD$ {item['monto']:,.2f}",
        ])
    if len(filas) == 1:
        filas.append(['--', '--', '--', 'Sin ventas en el periodo'])

    tabla_top = Table(filas, colWidths=[6 * cm, 4 * cm, 2.5 * cm, 3 * cm])
    tabla_top.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#21c99a')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
    ]))
    elementos.append(tabla_top)

    doc.build(elementos)
    buffer.seek(0)
    return buffer


def _totales_movimientos_stock(desde, hasta):
    inicio_dt, fin_dt = _rango_datetime(desde, hasta)
    resultado = MovimientoStock.objects.filter(
        fecha__gte=inicio_dt, fecha__lt=fin_dt
    ).aggregate(
        entradas=Coalesce(
            models.Sum('cantidad', filter=models.Q(tipo='Entrada')), models.Value(0),
            output_field=models.IntegerField(),
        ),
        salidas=Coalesce(
            models.Sum('cantidad', filter=models.Q(tipo='Salida')), models.Value(0),
            output_field=models.IntegerField(),
        ),
        total=models.Count('id'),
    )
    return resultado['entradas'], resultado['salidas'], resultado['total']

def _estado_actual_inventario():
    money_field = models.DecimalField(max_digits=18, decimal_places=2)
    resultado = Producto.objects.aggregate(
        valor_total=Coalesce(
            models.Sum(
                models.ExpressionWrapper(
                    models.F('costo_compra') * models.F('stock'),
                    output_field=money_field,
                )
            ),
            models.Value(Decimal('0'), output_field=money_field),
            output_field=money_field,
        ),
        bajo_stock=models.Count('id', filter=models.Q(stock__lt=models.F('stock_minimo'))),
    )
    return resultado['valor_total'], resultado['bajo_stock']

def _grafica_stock_ultimos_meses(hasta, cantidad=4):
    meses = []
    cursor = hasta.replace(day=1)
    for _ in range(cantidad):
        meses.append(cursor)
        if cursor.month == 1:
            cursor = cursor.replace(year=cursor.year - 1, month=12)
        else:
            cursor = cursor.replace(month=cursor.month - 1)
    meses.reverse()

    inicio_dt, fin_dt = _rango_datetime(meses[0], hasta)
    filas = (
        MovimientoStock.objects.filter(fecha__gte=inicio_dt, fecha__lt=fin_dt)
        .annotate(mes=TruncMonth('fecha'))
        .values('mes')
        .annotate(
            entradas=Coalesce(
                models.Sum('cantidad', filter=models.Q(tipo='Entrada')), models.Value(0),
                output_field=models.IntegerField(),
            ),
            salidas=Coalesce(
                models.Sum('cantidad', filter=models.Q(tipo='Salida')), models.Value(0),
                output_field=models.IntegerField(),
            ),
        )
    )
    por_mes = {
        (f['mes'].year, f['mes'].month): (f['entradas'], f['salidas'])
        for f in filas
    }
    datos = [
        {
            'mes': MESES_ES[mes.month],
            'entradas': por_mes.get((mes.year, mes.month), (0, 0))[0],
            'salidas': por_mes.get((mes.year, mes.month), (0, 0))[1],
        }
        for mes in meses
    ]
    valor_max = max([d['entradas'] for d in datos] + [d['salidas'] for d in datos] + [1])
    for d in datos:
        d['altura_entrada'] = int((d['entradas'] / valor_max) * 150) if valor_max else 0
        d['altura_salida'] = int((d['salidas'] / valor_max) * 150) if valor_max else 0
    return datos

def _productos_bajo_stock(top_n=10):
    return Producto.objects.filter(stock__lt=models.F('stock_minimo')).order_by('stock')[:top_n]


def _ventas_por_producto(desde, hasta):
    """Unidades vendidas por producto, agrupadas en PostgreSQL."""
    inicio_dt, fin_dt = _rango_datetime(desde, hasta)
    filas = (
        DetalleVenta.objects
        .filter(venta__fecha__gte=inicio_dt, venta__fecha__lt=fin_dt)
        .values('producto_id', 'producto__nombre', 'producto__sku')
        .annotate(unidades=models.Sum('cantidad'))
    )
    return {
        f['producto_id']: {
            'producto': SimpleNamespace(
                id=f['producto_id'], nombre=f['producto__nombre'], sku=f['producto__sku']
            ),
            'unidades': f['unidades'] or 0,
        }
        for f in filas
    }

def _generar_alertas_dashboard(top_n_stock=5):
    """Alertas reales de stock y variación mensual con consultas agregadas."""
    alertas_stock = [
        {
            'producto': producto,
            'mensaje': (
                f'Stock bajo: {producto.nombre} ({producto.sku}) tiene '
                f'{producto.stock} unidades disponibles. Stock mínimo: {producto.stock_minimo}.'
            ),
        }
        for producto in _productos_bajo_stock(top_n=top_n_stock)
    ]

    hoy = now().date()
    inicio_mes_actual = hoy.replace(day=1)
    fin_mes_anterior = inicio_mes_actual - timedelta(days=1)
    inicio_mes_anterior = fin_mes_anterior.replace(day=1)

    # Mes actual y anterior por producto en UNA consulta. Antes se hacía el
    # mismo GROUP BY dos veces contra Neon.
    inicio_anterior_dt, _ = _rango_datetime(inicio_mes_anterior, fin_mes_anterior)
    inicio_actual_dt, fin_actual_dt = _rango_datetime(inicio_mes_actual, hoy)
    filas_ventas = (
        DetalleVenta.objects
        .filter(venta__fecha__gte=inicio_anterior_dt, venta__fecha__lt=fin_actual_dt)
        .values('producto_id', 'producto__nombre', 'producto__sku')
        .annotate(
            unidades_anterior=Coalesce(
                models.Sum(
                    'cantidad',
                    filter=models.Q(venta__fecha__lt=inicio_actual_dt),
                ),
                models.Value(0),
                output_field=models.IntegerField(),
            ),
            unidades_actual=Coalesce(
                models.Sum(
                    'cantidad',
                    filter=models.Q(venta__fecha__gte=inicio_actual_dt),
                ),
                models.Value(0),
                output_field=models.IntegerField(),
            ),
        )
    )

    variaciones = []
    for fila in filas_ventas:
        unidades_anterior = fila['unidades_anterior'] or 0
        if unidades_anterior <= 0:
            continue
        unidades_actual = fila['unidades_actual'] or 0
        producto = SimpleNamespace(
            id=fila['producto_id'],
            nombre=fila['producto__nombre'],
            sku=fila['producto__sku'],
        )
        variacion_pct = ((unidades_actual - unidades_anterior) / unidades_anterior) * 100
        variaciones.append((producto, unidades_actual, unidades_anterior, variacion_pct))

    recomendaciones = []
    if variaciones:
        mayor_alza = max(variaciones, key=lambda v: v[3])
        mayor_baja = min(variaciones, key=lambda v: v[3])
        if mayor_alza[3] > 0:
            recomendaciones.append(
                f'{mayor_alza[0].nombre} tiene mayor demanda este mes: '
                f'{mayor_alza[1]} unidades vendidas vs {mayor_alza[2]} el mes anterior '
                f'(▲ {round(mayor_alza[3], 1)}%). Considere aumentar el stock.'
            )
        if mayor_baja[3] < 0:
            recomendaciones.append(
                f'La demanda de {mayor_baja[0].nombre} se encuentra en una caída: '
                f'{mayor_baja[1]} unidades vendidas vs {mayor_baja[2]} el mes anterior '
                f'(▼ {abs(round(mayor_baja[3], 1))}%).'
            )

    return {'alertas_stock': alertas_stock, 'recomendaciones': recomendaciones}


def _generar_pdf_inventario(desde, hasta, valor_total, bajo_stock, cantidad_entradas, cantidad_salidas, productos_bajo_stock):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    estilos = getSampleStyleSheet()
    elementos = []

    elementos.append(Paragraph("Cromf Finanzas - Reporte de Inventario", estilos['Title']))
    elementos.append(Paragraph(
        f"Movimientos del periodo: {desde.strftime('%d-%m-%Y')} al {hasta.strftime('%d-%m-%Y')}", estilos['Normal']
    ))
    elementos.append(Spacer(1, 0.5 * cm))

    tabla_kpi = Table([
        ['Valor Total del Inventario', f"RD$ {valor_total:,.2f}"],
        ['Productos con Bajo Stock', str(bajo_stock)],
        ['Entradas en el Periodo', str(cantidad_entradas)],
        ['Salidas en el Periodo', str(cantidad_salidas)],
    ], colWidths=[8 * cm, 6 * cm])
    tabla_kpi.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, 0), (0, -1), colors.whitesmoke),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elementos.append(tabla_kpi)
    elementos.append(Spacer(1, 1 * cm))

    elementos.append(Paragraph("Productos con Bajo Stock", estilos['Heading2']))
    filas = [['SKU', 'Producto', 'Categoria', 'Stock', 'Stock Minimo']]
    for p in productos_bajo_stock:
        filas.append([p.sku, p.nombre, p.categoria, str(p.stock), str(p.stock_minimo)])
    if len(filas) == 1:
        filas.append(['--', 'Ningun producto en bajo stock', '--', '--', '--'])

    tabla_bajo = Table(filas, colWidths=[2.2 * cm, 5 * cm, 3.5 * cm, 2 * cm, 3 * cm])
    tabla_bajo.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#21c99a')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
    ]))
    elementos.append(tabla_bajo)

    doc.build(elementos)
    buffer.seek(0)
    return buffer



# ============================================================
# IMPUESTOS
# Módulo incorporado sin reemplazar la lógica de Ventas/Compras/Finanzas.
# Solo lee las operaciones reales ya registradas y genera estimaciones/reportes.
# ============================================================

def _retencion_tarjetas(desde, hasta):
    ventas_tarjeta = Venta.objects.filter(
        fecha__date__gte=desde, fecha__date__lte=hasta, metodo_pago='Tarjeta',
    )
    return sum(
        ((v.total * Decimal('0.02')).quantize(Decimal('0.01')) for v in ventas_tarjeta),
        Decimal('0'),
    )


def _totales_impuestos_ventas_compras(desde, hasta):
    ventas_periodo = Venta.objects.filter(fecha__date__gte=desde, fecha__date__lte=hasta)
    compras_periodo = Compra.objects.filter(fecha__gte=desde, fecha__lte=hasta)

    ventas_gravadas = sum((v.subtotal for v in ventas_periodo), Decimal('0'))
    itbis_ventas = sum((v.itbs for v in ventas_periodo), Decimal('0'))
    credito_fiscal_compras = sum((c.impuestos for c in compras_periodo), Decimal('0'))
    retencion_tarjetas = _retencion_tarjetas(desde, hasta)
    itbis_neto = itbis_ventas - credito_fiscal_compras
    itbis_neto_despues_retencion = itbis_neto - retencion_tarjetas

    return {
        'ventas_gravadas': ventas_gravadas,
        'itbis_ventas': itbis_ventas,
        'credito_fiscal_compras': credito_fiscal_compras,
        'retencion_tarjetas': retencion_tarjetas,
        'itbis_neto': itbis_neto,
        'itbis_neto_despues_retencion': itbis_neto_despues_retencion,
    }


def _calcular_isr_empresarial(anio):
    desde = datetime(anio, 1, 1).date()
    hoy = now().date()
    hasta = hoy if hoy.year == anio else datetime(anio, 12, 31).date()
    total_ingresos, total_gastos = _totales_financieros(desde, hasta)
    renta_neta = total_ingresos - total_gastos
    isr_empresarial = (
        (renta_neta * Decimal('0.27')).quantize(Decimal('0.01'))
        if renta_neta > 0 else Decimal('0.00')
    )
    return {
        'total_ingresos_anio': total_ingresos,
        'total_gastos_anio': total_gastos,
        'renta_neta': renta_neta,
        'isr_empresarial': isr_empresarial,
    }


def _calcular_anticipo_isr(empresa):
    isr_anterior = Decimal(empresa.isr_ano_anterior or 0)
    return (isr_anterior / Decimal('12')).quantize(Decimal('0.01'))


def _generar_pdf_impuestos(desde, hasta, datos_itbis, datos_isr, anticipo_mensual):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    estilos = getSampleStyleSheet()
    elementos = [
        Paragraph('Cromf Finanzas - Reporte de Impuestos', estilos['Title']),
        Paragraph(
            f"Periodo ITBIS: {desde.strftime('%d-%m-%Y')} al {hasta.strftime('%d-%m-%Y')}",
            estilos['Normal'],
        ),
        Spacer(1, 0.5 * cm),
    ]

    tabla_itbis = Table([
        ['Ventas gravadas (subtotal)', f"RD$ {datos_itbis['ventas_gravadas']:,.2f}"],
        ['ITBIS generado en ventas', f"RD$ {datos_itbis['itbis_ventas']:,.2f}"],
        ['(-) Crédito fiscal de compras', f"RD$ {datos_itbis['credito_fiscal_compras']:,.2f}"],
        ['ITBIS neto antes de retención', f"RD$ {datos_itbis['itbis_neto']:,.2f}"],
        ['(-) Retención tarjetas (2%, informativa)', f"RD$ {datos_itbis['retencion_tarjetas']:,.2f}"],
        ['ITBIS neto después de retención', f"RD$ {datos_itbis['itbis_neto_despues_retencion']:,.2f}"],
    ], colWidths=[8 * cm, 6 * cm])
    tabla_itbis.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, 0), (0, -1), colors.whitesmoke),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elementos.extend([tabla_itbis, Spacer(1, 1 * cm)])

    elementos.append(Paragraph(f"ISR Empresarial - Año {hasta.year}", estilos['Heading2']))
    tabla_isr = Table([
        ['Ingresos del año', f"RD$ {datos_isr['total_ingresos_anio']:,.2f}"],
        ['Gastos del año', f"RD$ {datos_isr['total_gastos_anio']:,.2f}"],
        ['Renta neta estimada', f"RD$ {datos_isr['renta_neta']:,.2f}"],
        ['ISR empresarial estimado (27%)', f"RD$ {datos_isr['isr_empresarial']:,.2f}"],
        ['Anticipo mensual estimado', f"RD$ {anticipo_mensual:,.2f}"],
    ], colWidths=[8 * cm, 6 * cm])
    tabla_isr.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, 0), (0, -1), colors.whitesmoke),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elementos.append(tabla_isr)
    doc.build(elementos)
    buffer.seek(0)
    return buffer


def _solo_digitos(valor):
    return ''.join(ch for ch in str(valor or '') if ch.isdigit())


def _tipo_identificacion_607(valor):
    digitos = _solo_digitos(valor)
    if len(digitos) == 9:
        return 'RNC'
    if len(digitos) == 11:
        return 'CEDULA'
    return ''


def _referencia_fiscal_interna(venta):
    """Referencia de preparación; NO fabrica un e-NCF autorizado por DGII."""
    return f"{venta.tipo_comprobante}-{venta.numero_factura or ('VENTA-' + str(venta.pk))}"


def _ventas_pre607(desde, hasta):
    return list(
        Venta.objects.filter(fecha__date__gte=desde, fecha__date__lte=hasta)
        .select_related('cliente').order_by('fecha', 'id')
    )


def _contenido_pre607(ventas):
    columnas = [
        'TIPO_ID', 'RNC_CEDULA', 'REFERENCIA_FISCAL_INTERNA', 'FECHA',
        'SUBTOTAL', 'ITBIS', 'TOTAL', 'CONDICION_PAGO', 'METODO_PAGO',
        'RETENCION_TARJETA_2_PCT',
    ]
    lineas = ['|'.join(columnas)]
    for venta in ventas:
        identificacion = venta.cliente_rnc_cedula or (
            venta.cliente.rnc_cedula if venta.cliente_id and venta.cliente else ''
        )
        retencion = (venta.total * Decimal('0.02')).quantize(Decimal('0.01')) if venta.metodo_pago == 'Tarjeta' else Decimal('0.00')
        lineas.append('|'.join([
            _tipo_identificacion_607(identificacion),
            _solo_digitos(identificacion),
            _referencia_fiscal_interna(venta),
            venta.fecha.strftime('%Y%m%d'),
            f'{venta.subtotal:.2f}', f'{venta.itbs:.2f}', f'{venta.total:.2f}',
            venta.condicion_pago or '', venta.metodo_pago or '', f'{retencion:.2f}',
        ]))
    return '\n'.join(lineas) + '\n'


def _generar_pdf_pre607(desde, hasta, ventas):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=1.4*cm, bottomMargin=1.4*cm)
    estilos = getSampleStyleSheet()
    elementos = [
        Paragraph('Cromf Finanzas - Revisión interna Pre-607', estilos['Title']),
        Paragraph(
            f"Período: {desde.strftime('%d/%m/%Y')} al {hasta.strftime('%d/%m/%Y')}. "
            'Este documento sirve para preparación/revisión interna y no sustituye el archivo oficial validado por DGII.',
            estilos['Normal'],
        ),
        Spacer(1, 0.4*cm),
    ]
    filas = [['Referencia', 'Fecha', 'Cliente', 'RNC/Cédula', 'Subtotal', 'ITBIS', 'Total']]
    for venta in ventas:
        identificacion = venta.cliente_rnc_cedula or '--'
        filas.append([
            _referencia_fiscal_interna(venta), venta.fecha.strftime('%d/%m/%Y'),
            venta.cliente_nombre or 'Consumidor final', identificacion,
            f'{venta.subtotal:,.2f}', f'{venta.itbs:,.2f}', f'{venta.total:,.2f}',
        ])
    if len(filas) == 1:
        filas.append(['--', '--', 'Sin ventas', '--', '--', '--', '--'])
    tabla = Table(filas, colWidths=[3.1*cm,2.0*cm,3.5*cm,2.5*cm,2.2*cm,2.0*cm,2.3*cm], repeatRows=1)
    tabla.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#0f1524')),('TEXTCOLOR',(0,0),(-1,0),colors.white),
        ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,-1),7),
        ('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#E5E7EB')),
    ]))
    elementos.append(tabla)
    doc.build(elementos)
    buffer.seek(0)
    return buffer


@login_required
@requiere_permiso('impuestos')
def impuestos_607_txt(request):
    desde, hasta = _rango_por_defecto()
    try:
        if request.GET.get('desde'):
            desde = datetime.strptime(request.GET['desde'], '%Y-%m-%d').date()
        if request.GET.get('hasta'):
            hasta = datetime.strptime(request.GET['hasta'], '%Y-%m-%d').date()
    except ValueError:
        return HttpResponse('Fechas inválidas.', status=400, content_type='text/plain')
    ventas = _ventas_pre607(desde, hasta)
    empresa = DatosEmpresa.obtener()
    rnc = _solo_digitos(empresa.rnc) or 'SINRNC'
    response = HttpResponse(_contenido_pre607(ventas), content_type='text/plain; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="PRE607_{rnc}_{hasta:%m%Y}.txt"'
    return response


@login_required
@requiere_permiso('impuestos')
def impuestos_607_pdf(request):
    desde, hasta = _rango_por_defecto()
    try:
        if request.GET.get('desde'):
            desde = datetime.strptime(request.GET['desde'], '%Y-%m-%d').date()
        if request.GET.get('hasta'):
            hasta = datetime.strptime(request.GET['hasta'], '%Y-%m-%d').date()
    except ValueError:
        return HttpResponse('Fechas inválidas.', status=400, content_type='text/plain')
    ventas = _ventas_pre607(desde, hasta)
    buffer = _generar_pdf_pre607(desde, hasta, ventas)
    response = HttpResponse(buffer.read(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="pre607_{desde:%Y%m%d}_{hasta:%Y%m%d}.pdf"'
    return response


@login_required
@requiere_permiso('impuestos')
def impuestos(request):
    puede_crear = puede_escribir_en(request, 'impuestos')
    empresa = DatosEmpresa.obtener()

    desde, hasta = _rango_por_defecto()
    desde_str = request.POST.get('desde') if request.method == 'POST' else request.GET.get('desde')
    hasta_str = request.POST.get('hasta') if request.method == 'POST' else request.GET.get('hasta')
    try:
        if desde_str:
            desde = datetime.strptime(desde_str, '%Y-%m-%d').date()
        if hasta_str:
            hasta = datetime.strptime(hasta_str, '%Y-%m-%d').date()
    except ValueError:
        messages.error(request, 'Las fechas indicadas no son válidas.')
        return redirect('impuestos')

    datos_itbis = _totales_impuestos_ventas_compras(desde, hasta)
    datos_isr = _calcular_isr_empresarial(hasta.year)
    anticipo_mensual = _calcular_anticipo_isr(empresa)
    ventas_607 = _ventas_pre607(desde, hasta)
    compras_606 = list(
        Compra.objects.filter(fecha__gte=desde, fecha__lte=hasta)
        .select_related('proveedor').order_by('fecha', 'id')
    )
    totales_606 = {
        'subtotal': sum((c.subtotal for c in compras_606), Decimal('0')),
        'itbis': sum((c.impuestos for c in compras_606), Decimal('0')),
        'total': sum((c.total for c in compras_606), Decimal('0')),
    }

    if request.method == 'POST':
        if not puede_crear:
            messages.error(request, 'No tienes permiso para generar reportes de impuestos.')
            return redirect('impuestos')
        buffer = _generar_pdf_impuestos(desde, hasta, datos_itbis, datos_isr, anticipo_mensual)
        nombre_archivo = f"reporte_impuestos_{desde:%Y%m%d}_{hasta:%Y%m%d}.pdf"
        reporte = ReporteGenerado(
            nombre=f"Reporte de Impuestos {desde:%d-%m-%Y} al {hasta:%d-%m-%Y}",
            tipo='Impuestos', fecha_desde=desde, fecha_hasta=hasta, formato='PDF',
        )
        reporte.archivo.save(nombre_archivo, ContentFile(buffer.read()), save=True)
        messages.success(request, 'Reporte de Impuestos generado correctamente.')
        return redirect(f"{reverse('impuestos')}?tab=isr&desde={desde}&hasta={hasta}")

    return render(request, 'core/impuestos.html', {
        'puede_crear': puede_crear,
        'empresa': empresa,
        'desde': desde,
        'hasta': hasta,
        'datos_itbis': datos_itbis,
        'datos_isr': datos_isr,
        'anticipo_mensual': anticipo_mensual,
        'anio_actual': hasta.year,
        'reportes_impuestos': ReporteGenerado.objects.filter(tipo='Impuestos')[:10],
        'ventas_607': ventas_607,
        'compras_606': compras_606,
        'totales_606': totales_606,
    })


# ============================================================
# MODO IA
# Predicción local (Random Forest) + chat opcional mediante ANTHROPIC_API_KEY.
# Las dependencias se importan de forma diferida para que el resto del sistema
# siga arrancando incluso si aún no se instalaron los paquetes de IA.
# ============================================================

def _resumen_ia_seguro():
    try:
        from .ml.prediccion import generar_resumen_negocio
        return generar_resumen_negocio()
    except ImportError as exc:
        return {
            'disponible': False,
            'mensaje': (
                'Faltan dependencias del módulo de IA. Ejecuta '
                'python -m pip install -r requirements.txt. '
                f'Detalle: {exc}'
            ),
        }
    except Exception as exc:
        return {'disponible': False, 'mensaje': f'No se pudieron generar las predicciones: {exc}'}


def _construir_contexto_negocio(resumen):
    if not resumen.get('disponible'):
        return f"Predicciones no disponibles: {resumen.get('mensaje', 'sin datos suficientes')}."

    lineas = [
        f"Ingresos proyectados para los próximos 30 días: RD$ {resumen['ingresos_proyectados_30_dias']:,.2f}",
        f"Productos con alerta de reabastecimiento: {resumen['total_alertas_reabastecimiento']}",
        '',
        'Top de productos por demanda proyectada:',
    ]
    for item in resumen.get('top_5_productos', []):
        lineas.append(
            f"- {item['producto'].nombre}: {item['proyeccion_30_dias']} unidades proyectadas; "
            f"stock actual {item['producto'].stock}."
        )
    if resumen.get('alertas_reabastecimiento'):
        lineas.append('')
        lineas.append('Alertas de reabastecimiento:')
        for item in resumen['alertas_reabastecimiento']:
            dias = item.get('dias_hasta_agotarse')
            dias_texto = f'{dias} días' if dias is not None else 'sin estimación'
            lineas.append(
                f"- {item['producto'].nombre}: stock {item['producto'].stock}, "
                f"agotamiento {dias_texto}, compra sugerida {item['cantidad_sugerida_compra']} unidades."
            )
    return '\n'.join(lineas)


def _respuesta_ia_local(resumen):
    """Respuesta de respaldo transparente cuando no hay API externa configurada."""
    if not resumen.get('disponible'):
        return (
            'El asistente externo no está configurado y las predicciones locales tampoco '
            f"están disponibles todavía. {resumen.get('mensaje', '')}"
        ).strip()
    top = resumen.get('top_5_productos') or []
    alertas = resumen.get('alertas_reabastecimiento') or []
    partes = [
        'El asistente externo no está configurado; este es un resumen automático de los datos locales.',
        f"Ingresos proyectados a 30 días: RD$ {resumen.get('ingresos_proyectados_30_dias', 0):,.2f}.",
        f"Alertas de reabastecimiento: {resumen.get('total_alertas_reabastecimiento', 0)}.",
    ]
    if top:
        partes.append(
            f"Mayor demanda proyectada: {top[0]['producto'].nombre} "
            f"({top[0]['proyeccion_30_dias']} unidades en 30 días)."
        )
    if alertas:
        partes.append(
            f"Prioridad de reabastecimiento: {alertas[0]['producto'].nombre}, "
            f"sugerencia {alertas[0]['cantidad_sugerida_compra']} unidades."
        )
    return ' '.join(partes)


@login_required
@requiere_permiso('modo_ia')
def modo_ia(request):
    resumen = _resumen_ia_seguro()

    if request.method == 'POST' and request.POST.get('form_tipo') == 'pregunta_ia':
        pregunta = request.POST.get('pregunta', '').strip()
        if not pregunta:
            messages.error(request, 'Escribe una pregunta para el asistente.')
            return redirect('modo_ia')

        respuesta_texto = None
        usa_api = bool(getattr(settings, 'ANTHROPIC_API_KEY', ''))
        if usa_api:
            try:
                from anthropic import Anthropic
                contexto = _construir_contexto_negocio(resumen)
                client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
                mensaje = client.messages.create(
                    model='claude-haiku-4-5-20251001',
                    max_tokens=500,
                    system=(
                        'Eres el asistente de IA de Cromf Finanzas. Responde en español, '
                        'de forma breve y clara, usando SOLO los datos del negocio que se '
                        'te proporcionan. Si no hay datos suficientes, indícalo sin inventar cifras.'
                    ),
                    messages=[{'role': 'user', 'content': f'{contexto}\n\nPregunta: {pregunta}'}],
                )
                respuesta_texto = mensaje.content[0].text
            except Exception as exc:
                messages.warning(request, f'No se pudo usar el asistente externo; se mostrará un resumen local. Detalle: {exc}')

        if not respuesta_texto:
            respuesta_texto = _respuesta_ia_local(resumen)

        ConsultaIA.objects.create(
            usuario=request.user,
            pregunta=pregunta,
            respuesta=respuesta_texto,
        )
        messages.success(request, 'Consulta registrada.')
        return redirect('modo_ia')

    return render(request, 'core/modo_ia.html', {
        'resumen': resumen,
        'historial_consultas': ConsultaIA.objects.filter(usuario=request.user)[:10],
        'ia_api_configurada': bool(getattr(settings, 'ANTHROPIC_API_KEY', '')),
    })

@login_required
@requiere_permiso('reportes')
def reportes(request):
    puede_crear = puede_escribir_en(request, 'reportes')

    if request.method == 'POST':
        if not puede_crear:
            messages.error(request, 'No tienes permiso para generar reportes.')
            return redirect('reportes')

        tipo_reporte = request.POST.get('tipo_reporte', 'Financiero')
        desde_str = request.POST.get('desde')
        hasta_str = request.POST.get('hasta')

        desde_default, hasta_default = _rango_por_defecto()
        try:
            desde = datetime.strptime(desde_str, '%Y-%m-%d').date() if desde_str else desde_default
            hasta = datetime.strptime(hasta_str, '%Y-%m-%d').date() if hasta_str else hasta_default
        except ValueError:
            messages.error(request, 'Las fechas del reporte no son válidas.')
            return redirect('reportes')

        if tipo_reporte == 'Ventas':
            total_vendido, cantidad_ventas = _totales_ventas(desde, hasta)
            ticket_promedio = (total_vendido / cantidad_ventas) if cantidad_ventas else Decimal('0')
            top_productos = _top_productos_vendidos(desde, hasta, top_n=10)

            buffer = _generar_pdf_ventas(desde, hasta, total_vendido, cantidad_ventas, ticket_promedio, top_productos)
            nombre_archivo = f"reporte_ventas_{desde.strftime('%Y%m%d')}_{hasta.strftime('%Y%m%d')}.pdf"
            reporte = ReporteGenerado(
                nombre=f"Reporte de Ventas {desde.strftime('%d-%m-%Y')} al {hasta.strftime('%d-%m-%Y')}",
                tipo='Ventas', fecha_desde=desde, fecha_hasta=hasta, formato='PDF',
            )
            reporte.archivo.save(nombre_archivo, ContentFile(buffer.read()), save=True)
            return redirect(f"{reverse('reportes')}?tab=ventas&desde={desde}&hasta={hasta}")

        elif tipo_reporte == 'Inventario':
            valor_total, bajo_stock = _estado_actual_inventario()
            cantidad_entradas, cantidad_salidas, _ = _totales_movimientos_stock(desde, hasta)
            productos_bajo_stock = _productos_bajo_stock(top_n=20)

            buffer = _generar_pdf_inventario(desde, hasta, valor_total, bajo_stock, cantidad_entradas, cantidad_salidas, productos_bajo_stock)
            nombre_archivo = f"reporte_inventario_{desde.strftime('%Y%m%d')}_{hasta.strftime('%Y%m%d')}.pdf"
            reporte = ReporteGenerado(
                nombre=f"Reporte de Inventario {desde.strftime('%d-%m-%Y')} al {hasta.strftime('%d-%m-%Y')}",
                tipo='Inventario', fecha_desde=desde, fecha_hasta=hasta, formato='PDF',
            )
            reporte.archivo.save(nombre_archivo, ContentFile(buffer.read()), save=True)
            return redirect(f"{reverse('reportes')}?tab=inventario&desde={desde}&hasta={hasta}")

        else:  # Financiero
            total_ingresos, total_gastos = _totales_financieros(desde, hasta)
            movimientos = list(MovimientoFinanciero.objects.filter(fecha__gte=desde, fecha__lte=hasta))
            movimientos.extend(_movimientos_nomina(desde, hasta))
            movimientos.sort(key=lambda m: m.fecha, reverse=True)

            buffer = _generar_pdf_financiero(desde, hasta, total_ingresos, total_gastos, movimientos)
            nombre_archivo = f"reporte_financiero_{desde.strftime('%Y%m%d')}_{hasta.strftime('%Y%m%d')}.pdf"
            reporte = ReporteGenerado(
                nombre=f"Reporte Financiero {desde.strftime('%d-%m-%Y')} al {hasta.strftime('%d-%m-%Y')}",
                tipo='Financiero', fecha_desde=desde, fecha_hasta=hasta, formato='PDF',
            )
            reporte.archivo.save(nombre_archivo, ContentFile(buffer.read()), save=True)
            return redirect(f"{reverse('reportes')}?desde={desde}&hasta={hasta}")

    desde_str = request.GET.get('desde')
    hasta_str = request.GET.get('hasta')
    if desde_str and hasta_str:
        try:
            desde = datetime.strptime(desde_str, '%Y-%m-%d').date()
            hasta = datetime.strptime(hasta_str, '%Y-%m-%d').date()
        except ValueError:
            desde, hasta = _rango_por_defecto()
    else:
        desde, hasta = _rango_por_defecto()

    desde_anterior = desde - timedelta(days=(hasta - desde).days + 1)
    hasta_anterior = desde - timedelta(days=1)

    total_ingresos, total_gastos = _totales_financieros(desde, hasta)
    utilidad = total_ingresos - total_gastos

    ingresos_anterior, gastos_anterior = _totales_financieros(desde_anterior, hasta_anterior)
    utilidad_anterior = ingresos_anterior - gastos_anterior

    tendencia_ingresos, ingresos_positiva = _tendencia(total_ingresos, ingresos_anterior)
    tendencia_gastos, gastos_positiva = _tendencia(total_gastos, gastos_anterior)
    tendencia_utilidad, utilidad_positiva = _tendencia(utilidad, utilidad_anterior)

    grafica_financiero, eje_y_financiero = _grafica_financiero_ultimos_meses(hasta)
    reportes_financiero = ReporteGenerado.objects.all()[:20]

    total_vendido, cantidad_ventas = _totales_ventas(desde, hasta)
    ticket_promedio = (total_vendido / cantidad_ventas) if cantidad_ventas else Decimal('0')

    total_vendido_anterior, cantidad_ventas_anterior = _totales_ventas(desde_anterior, hasta_anterior)
    ticket_promedio_anterior = (total_vendido_anterior / cantidad_ventas_anterior) if cantidad_ventas_anterior else Decimal('0')

    tendencia_vendido, vendido_positiva = _tendencia(total_vendido, total_vendido_anterior)
    tendencia_cantidad, cantidad_positiva = _tendencia(cantidad_ventas, cantidad_ventas_anterior)
    tendencia_ticket, ticket_positiva = _tendencia(ticket_promedio, ticket_promedio_anterior)

    grafica_ventas = _grafica_ventas_ultimos_meses(hasta)
    top_productos_vendidos = _top_productos_vendidos(desde, hasta, top_n=5)
    reportes_ventas = ReporteGenerado.objects.filter(tipo='Ventas')[:20]

    valor_total_inventario, bajo_stock_count = _estado_actual_inventario()
    cantidad_entradas, cantidad_salidas, total_movimientos = _totales_movimientos_stock(desde, hasta)

    cantidad_entradas_anterior, cantidad_salidas_anterior, _ = _totales_movimientos_stock(desde_anterior, hasta_anterior)
    tendencia_entradas, entradas_positiva = _tendencia(cantidad_entradas, cantidad_entradas_anterior)
    tendencia_salidas, salidas_positiva = _tendencia(cantidad_salidas, cantidad_salidas_anterior)

    grafica_stock = _grafica_stock_ultimos_meses(hasta)
    productos_bajo_stock = _productos_bajo_stock(top_n=10)
    reportes_inventario = ReporteGenerado.objects.filter(tipo='Inventario')[:20]

    return render(request, 'core/reportes.html', {
        'desde': desde,
        'hasta': hasta,
        'puede_crear': puede_crear,

        'total_ingresos': total_ingresos,
        'total_gastos': total_gastos,
        'utilidad': utilidad,
        'tendencia_ingresos': tendencia_ingresos,
        'ingresos_positiva': ingresos_positiva,
        'tendencia_gastos': tendencia_gastos,
        'gastos_positiva': gastos_positiva,
        'tendencia_utilidad': tendencia_utilidad,
        'utilidad_positiva': utilidad_positiva,
        'grafica': grafica_financiero,
        'eje_y_financiero': eje_y_financiero,
        'reportes_generados': reportes_financiero,

        'total_vendido': total_vendido,
        'cantidad_ventas': cantidad_ventas,
        'ticket_promedio': ticket_promedio,
        'tendencia_vendido': tendencia_vendido,
        'vendido_positiva': vendido_positiva,
        'tendencia_cantidad': tendencia_cantidad,
        'cantidad_positiva': cantidad_positiva,
        'tendencia_ticket': tendencia_ticket,
        'ticket_positiva': ticket_positiva,
        'grafica_ventas': grafica_ventas,
        'top_productos_vendidos': top_productos_vendidos,
        'reportes_ventas': reportes_ventas,

        'valor_total_inventario': valor_total_inventario,
        'bajo_stock_count': bajo_stock_count,
        'cantidad_entradas': cantidad_entradas,
        'cantidad_salidas': cantidad_salidas,
        'tendencia_entradas': tendencia_entradas,
        'entradas_positiva': entradas_positiva,
        'tendencia_salidas': tendencia_salidas,
        'salidas_positiva': salidas_positiva,
        'grafica_stock': grafica_stock,
        'productos_bajo_stock': productos_bajo_stock,
        'reportes_inventario': reportes_inventario,
    })

@login_required
@requiere_permiso('configuracion')
def configuracion(request):
    empleado = getattr(request.user, 'empleado', None)
    empresa = DatosEmpresa.obtener()
    preferencias, _ = PreferenciasNotificacion.objects.get_or_create(usuario=request.user)

    perfil_form = PerfilForm(instance=request.user)
    password_form = PasswordChangeForm(request.user)
    empresa_form = DatosEmpresaForm(instance=empresa)
    preferencias_form = PreferenciasNotificacionForm(instance=preferencias)

    if request.method == 'POST':
        tipo = request.POST.get('form_tipo')
        if tipo == 'perfil':
            perfil_form = PerfilForm(request.POST, instance=request.user)
            if perfil_form.is_valid():
                perfil_form.save()
                if empleado:
                    empleado.telefono = request.POST.get('telefono', empleado.telefono)
                    empleado.save(update_fields=['telefono'])
                messages.success(request, 'Perfil actualizado correctamente.')
                return redirect('configuracion')
        elif tipo == 'password':
            password_form = PasswordChangeForm(request.user, request.POST)
            if password_form.is_valid():
                usuario_actualizado = password_form.save()
                update_session_auth_hash(request, usuario_actualizado)
                messages.success(request, 'Contraseña actualizada correctamente.')
                return redirect('configuracion')
        elif tipo == 'empresa':
            empresa_form = DatosEmpresaForm(request.POST, instance=empresa)
            if empresa_form.is_valid():
                empresa_form.save()
                messages.success(request, 'Datos de la empresa actualizados correctamente.')
                return redirect('configuracion')
        elif tipo == 'notificaciones':
            preferencias_form = PreferenciasNotificacionForm(request.POST, instance=preferencias)
            if preferencias_form.is_valid():
                preferencias_form.save()
                messages.success(request, 'Preferencias actualizadas correctamente.')
                return redirect('configuracion')

    return render(request, 'core/configuracion.html', {
        'perfil_form': perfil_form, 'password_form': password_form,
        'empresa_form': empresa_form, 'preferencias_form': preferencias_form,
        'empleado': empleado,
    })



# NOTA: la vista resetear_ventas_mes fue eliminada intencionalmente.
# Las ventas son historial permanente del negocio; no debe existir
# ninguna funcionalidad que las borre masivamente (ni a ellas ni a
# los movimientos financieros/de inventario que generan). Cualquier
# corrección futura sobre una venta debe hacerse mediante un
# mecanismo explícito de anulación/corrección auditado, no por borrado.


# API de venta real: guarda Venta + DetalleVenta + movimientos y descuenta stock.
@login_required
@requiere_permiso('ventas')
def procesar_venta(request):
    """Registra una venta completa, con snapshots, fiscalidad y CxC.

    El navegador solo manda intención (SKU, cantidad, descuento y datos de
    operación). Precio, costo, ITBIS, totales y stock se recalculan en el
    servidor dentro de transaction.atomic().
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Metodo no permitido'}, status=405)
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON invalido'}, status=400)
    productos = data.get('productos') or []
    if not productos:
        return JsonResponse({'error': 'La venta debe contener productos'}, status=400)
    if not puede_escribir_en(request, 'ventas'):
        return JsonResponse({'error': 'No tienes permiso para procesar ventas'}, status=403)

    try:
        with transaction.atomic():
            cliente = None
            cliente_id = data.get('cliente_id')
            if cliente_id:
                cliente = Cliente.objects.filter(pk=cliente_id, activo=True).first()
                if cliente is None:
                    raise ValueError('El cliente seleccionado no existe o está inactivo.')

            # La interfaz expone una sola decisión: Efectivo, Tarjeta o Crédito.
            # Internamente conservamos condicion_pago para la trazabilidad y CxC.
            forma_pago_raw = _normalizar_texto(
                data.get('forma_pago') or data.get('medio_pago') or data.get('metodo_pago') or 'Efectivo'
            )
            if forma_pago_raw == 'credito':
                metodo = 'Credito'
                condicion = 'Credito'
            elif forma_pago_raw == 'tarjeta':
                metodo = 'Tarjeta'
                condicion = 'Contado'
            else:
                metodo = 'Efectivo'
                condicion = 'Contado'

            if condicion == 'Credito' and cliente is None:
                raise ValueError('Una venta a crédito requiere seleccionar un cliente registrado.')

            # El comprobante se determina automáticamente según el cliente;
            # ya no es una decisión manual en la pantalla de ventas.
            tipo_comprobante = _resolver_tipo_comprobante(cliente)
            tasa_empresa = Decimal(str(DatosEmpresa.obtener().itbs_porcentaje))
            lineas = []
            subtotal_bruto = Decimal('0')
            descuento_total = Decimal('0')
            subtotal = Decimal('0')
            itbs = Decimal('0')

            for item in productos:
                sku = str(item.get('sku', '')).strip()
                try:
                    cantidad = int(item.get('cantidad', 0))
                except (TypeError, ValueError):
                    cantidad = 0
                if cantidad <= 0:
                    raise ValueError('Cantidad inválida.')
                try:
                    descuento_pct = Decimal(str(item.get('descuento_porcentaje', 0) or 0))
                except (InvalidOperation, TypeError, ValueError):
                    raise ValueError('Descuento inválido.')
                if descuento_pct < 0 or descuento_pct > 100:
                    raise ValueError('El descuento debe estar entre 0 y 100.')

                producto = Producto.objects.select_for_update().get(sku=sku)
                if cantidad > producto.stock:
                    raise ValueError(
                        f'Stock insuficiente para {producto.nombre}. Disponible: {producto.stock}'
                    )
                precio = producto.precio_venta
                calculo = _calcular_linea_fiscal(
                    producto, cantidad, precio, descuento_pct, cliente, tasa_empresa
                )
                lineas.append((producto, cantidad, precio, descuento_pct, calculo))
                subtotal_bruto += calculo['importe_bruto']
                descuento_total += calculo['descuento']
                subtotal += calculo['subtotal']
                itbs += calculo['itbs']

            total = (subtotal + itbs).quantize(Decimal('0.01'))
            venta = Venta.objects.create(
                cliente=cliente,
                subtotal_bruto=subtotal_bruto,
                descuento=descuento_total,
                subtotal=subtotal,
                itbs=itbs,
                total=total,
                metodo_pago=metodo,
                condicion_pago=condicion,
                tipo_comprobante=tipo_comprobante,
                vendedor=request.user,
                observaciones='',
                **_snapshot_cliente(cliente),
            )

            for producto, cantidad, precio, descuento_pct, calculo in lineas:
                DetalleVenta.objects.create(
                    venta=venta,
                    producto=producto,
                    cantidad=cantidad,
                    precio_unitario=precio,
                    costo_unitario=producto.costo_compra,
                    descuento_porcentaje=descuento_pct,
                    tasa_itbs=calculo['tasa_itbs'],
                    itbs_monto=calculo['itbs'],
                )
                producto.stock -= cantidad
                producto.save(update_fields=['stock'])
                MovimientoStock.objects.create(
                    producto=producto,
                    tipo='Salida',
                    cantidad=cantidad,
                    motivo=f'Venta {venta.numero_factura}',
                )

            if condicion == 'Contado':
                MovimientoFinanciero.objects.create(
                    tipo='Ingreso',
                    fecha=venta.fecha.date(),
                    categoria='Ventas',
                    cliente_proveedor=venta.cliente_nombre,
                    monto=total,
                    medio_pago=metodo,
                    cuenta=_cuenta_automatica_por_medio(metodo),
                    factura=venta.numero_factura,
                    venta=venta,
                )
            else:
                dias_credito = DatosEmpresa.obtener().dias_credito
                CuentaPorCobrar.objects.create(
                    venta=venta,
                    cliente=cliente,
                    importe_original=total,
                    fecha_emision=venta.fecha.date(),
                    fecha_vencimiento=venta.fecha.date() + timedelta(days=dias_credito),
                    creado_por=request.user,
                )

        return JsonResponse({
            'mensaje': (
                'Venta a crédito registrada. El saldo quedó en Cuentas por Cobrar.'
                if condicion == 'Credito' else '¡Venta guardada en la base de datos!'
            ),
            'venta_id': venta.id,
            'numero_factura': venta.numero_factura,
            'tipo_comprobante': venta.tipo_comprobante,
            'subtotal': float(subtotal),
            'itbs': float(itbs),
            'total': float(total),
            'credito': condicion == 'Credito',
            'factura_url': reverse('venta_factura', args=[venta.id]),
        })
    except Producto.DoesNotExist:
        return JsonResponse({'error': 'Uno de los productos no existe'}, status=400)
    except (ValueError, TypeError, InvalidOperation) as exc:
        return JsonResponse({'error': str(exc)}, status=400)


@login_required
def clientes_crear_ajax(request):
    """Alta rápida desde Ventas usando exactamente ClienteForm."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Metodo no permitido'}, status=405)
    if not puede_escribir_en(request, 'clientes'):
        return JsonResponse({'error': 'No tienes permiso para crear clientes'}, status=403)
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON invalido'}, status=400)
    data.setdefault('tipo', 'Publico')
    data.setdefault('condicion_fiscal', 'Normal')
    data.setdefault('activo', True)
    form = ClienteForm(data)
    if not form.is_valid():
        primer_error = next(iter(form.errors.values()))[0]
        return JsonResponse({'error': primer_error, 'errores': form.errors}, status=400)
    cliente = form.save()
    return JsonResponse({
        'id': cliente.id,
        'codigo': cliente.codigo,
        'nombre': cliente.nombre,
        'tipo': cliente.tipo,
        'condicion_fiscal': cliente.condicion_fiscal,
        'rnc_cedula': cliente.rnc_cedula,
        'telefono': cliente.telefono,
        'email': cliente.email,
        'direccion': cliente.direccion,
    })


@login_required
@requiere_permiso('ventas')
def venta_factura(request, venta_id):
    venta = get_object_or_404(
        Venta.objects.select_related('cliente', 'pedido', 'vendedor').prefetch_related('detalles__producto'),
        pk=venta_id,
    )
    return render(request, 'core/venta_factura.html', {
        'venta': venta,
        'empresa': DatosEmpresa.obtener(),
        'modo_impresion': False,
    })


@login_required
@requiere_permiso('ventas')
def factura_imprimir(request, venta_id):
    """Vista limpia para imprimir desde el navegador (novedad de Rosa)."""
    venta = get_object_or_404(
        Venta.objects.select_related('cliente', 'pedido', 'vendedor').prefetch_related('detalles__producto'),
        pk=venta_id,
    )
    return render(request, 'core/venta_factura.html', {
        'venta': venta,
        'empresa': DatosEmpresa.obtener(),
        'modo_impresion': True,
    })


@login_required
@requiere_permiso('ventas')
def venta_factura_pdf(request, venta_id):
    venta = get_object_or_404(
        Venta.objects.select_related('cliente', 'pedido', 'vendedor').prefetch_related('detalles__producto'),
        pk=venta_id,
    )
    buffer = _generar_pdf_factura(venta, DatosEmpresa.obtener())
    response = HttpResponse(buffer.read(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{venta.numero_factura}.pdf"'
    return response


def _generar_pdf_factura(venta, empresa):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
    )
    estilos = getSampleStyleSheet()
    oscuro = colors.HexColor('#0f1524')
    acento = colors.HexColor('#21c99a')
    gris = colors.HexColor('#6B7280')
    titulo = ParagraphStyle('FacturaTitulo', parent=estilos['Normal'], fontSize=20, fontName='Helvetica-Bold', textColor=acento, alignment=2)
    empresa_nombre = ParagraphStyle('EmpresaNombre', parent=estilos['Normal'], fontSize=14, fontName='Helvetica-Bold', textColor=oscuro)
    texto = ParagraphStyle('TextoFactura', parent=estilos['Normal'], fontSize=8.5, textColor=gris, leading=12)
    normal = ParagraphStyle('NormalFactura', parent=estilos['Normal'], fontSize=9, textColor=oscuro, leading=12)

    empresa_lineas = [f'RNC: {empresa.rnc}' if empresa.rnc else '', empresa.direccion or '', empresa.telefono or '', empresa.email or '']
    encabezado = Table([[
        [Paragraph(empresa.nombre_comercial, empresa_nombre), Paragraph('<br/>'.join(x for x in empresa_lineas if x), texto)],
        [Paragraph('FACTURA', titulo), Paragraph(f'{venta.numero_factura}<br/>{venta.tipo_comprobante}<br/>{venta.fecha.strftime("%d/%m/%Y %H:%M")}', normal)],
    ]], colWidths=[10.5 * cm, 7 * cm])
    encabezado.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'TOP'), ('LINEBELOW', (0,0), (-1,0), 1.2, oscuro)]))

    elementos = [encabezado, Spacer(1, 0.35 * cm)]
    elementos.append(Paragraph('<b>Cliente</b>', normal))
    cliente_info = [venta.cliente_nombre or 'Cliente Genérico']
    if venta.cliente_rnc_cedula:
        cliente_info.append(f'RNC/Cédula: {venta.cliente_rnc_cedula}')
    if venta.cliente_direccion:
        cliente_info.append(venta.cliente_direccion)
    if venta.cliente_telefono:
        cliente_info.append(f'Tel: {venta.cliente_telefono}')
    if venta.cliente_email:
        cliente_info.append(venta.cliente_email)
    elementos.append(Paragraph('<br/>'.join(cliente_info), texto))
    elementos.append(Spacer(1, 0.35 * cm))

    filas = [['Cant.', 'Descripción', 'Precio base', 'Desc.', 'ITBIS', 'Total línea']]
    for d in venta.detalles.all():
        total_linea = d.subtotal + d.itbs_monto
        filas.append([
            str(d.cantidad), d.producto.nombre,
            f'RD$ {d.precio_unitario:,.2f}',
            f'RD$ {d.descuento_monto:,.2f}' if d.descuento_porcentaje else '--',
            f'RD$ {d.itbs_monto:,.2f}',
            f'RD$ {total_linea:,.2f}',
        ])
    detalle = Table(filas, colWidths=[1.4*cm, 5.6*cm, 2.8*cm, 2.3*cm, 2.2*cm, 2.8*cm], repeatRows=1)
    detalle.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), oscuro), ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,-1), 8.3),
        ('GRID', (0,0), (-1,-1), 0.35, colors.HexColor('#E5E7EB')),
        ('ALIGN', (0,0), (0,-1), 'CENTER'), ('ALIGN', (2,1), (-1,-1), 'RIGHT'),
    ]))
    elementos.extend([detalle, Spacer(1, 0.35 * cm)])

    totales = [
        ['Subtotal bruto', f'RD$ {venta.subtotal_bruto:,.2f}'],
        ['Descuentos', f'RD$ {venta.descuento:,.2f}'],
        ['Subtotal', f'RD$ {venta.subtotal:,.2f}'],
        ['ITBIS', f'RD$ {venta.itbs:,.2f}'],
        ['TOTAL', f'RD$ {venta.total:,.2f}'],
    ]
    tabla_totales = Table(totales, colWidths=[4.5*cm, 3.5*cm], hAlign='RIGHT')
    tabla_totales.setStyle(TableStyle([
        ('ALIGN', (1,0), (-1,-1), 'RIGHT'), ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
        ('LINEABOVE', (0,-1), (-1,-1), 1, oscuro), ('TEXTCOLOR', (0,-1), (-1,-1), acento),
    ]))
    elementos.append(tabla_totales)
    elementos.append(Spacer(1, 0.25 * cm))
    elementos.append(Paragraph(
        f'Forma de pago: {venta.forma_pago}'
        + (f' · Vendedor: {venta.vendedor.username}' if venta.vendedor else ''), texto
    ))
    if venta.observaciones:
        elementos.append(Paragraph(f'Observaciones: {venta.observaciones}', texto))
    doc.build(elementos)
    buffer.seek(0)
    return buffer


@login_required
@requiere_permiso('ventas')
def guardar_historial_producto(request):
    # El historial ya no usa ProductoVentaHistorial: los productos reales son la fuente.
    if request.method != 'POST':
        return JsonResponse({'error':'Metodo no permitido'}, status=405)
    return JsonResponse({'mensaje':'El producto ya forma parte del catalogo real'})


@login_required
@requiere_permiso('ventas')
def borrar_historial_producto(request):
    # No elimina productos del catalogo; la UI antigua puede seguir llamando este endpoint.
    if request.method != 'POST':
        return JsonResponse({'error':'Metodo no permitido'}, status=405)
    return JsonResponse({'mensaje':'El historial visual usa productos reales y no se elimina del catalogo'})


# ============================================================
# GESTIÓN DE USUARIOS (solo Administrador)
# ============================================================
# Usa exclusivamente User, Group y los formularios nativos de
# Django (UserCreationForm, AdminPasswordChangeForm). No hay
# eliminación física de usuarios: se desactivan (user.is_active).

def _rol_de(usuario):
    """Devuelve el nombre del primer grupo del usuario, o None."""
    grupo = usuario.groups.first()
    return grupo.name if grupo else None


@permiso_modulo_requerido('usuarios')
def usuarios_lista(request):
    lista_usuarios = User.objects.all().order_by('username')

    return render(request, 'core/usuarios.html', {
        'usuarios': [
            {
                'objeto': u,
                'rol': _rol_de(u),
                'es_administrador': es_administrador(u),
            }
            for u in lista_usuarios
        ],
        # Controla si se muestra la pestaña "Roles y Permisos" (Fase 2).
        # No reemplaza la protección real, que está en cada vista de
        # roles_* mediante @permission_required('core.gestionar_roles').
        'puede_gestionar_roles': request.user.has_perm('core.gestionar_roles'),
        'puede_gestionar_recuperaciones': request.user.has_perm('core.gestionar_recuperaciones'),
    })


@permiso_modulo_requerido('usuarios')
def usuarios_crear(request):
    if request.method == 'POST':
        form = CrearUsuarioForm(request.POST)

        if form.is_valid():
            nuevo_usuario = form.save()
            nuevo_usuario.groups.add(form.cleaned_data['grupo'])
            messages.success(request, 'Usuario creado correctamente.')
            return redirect('usuarios_lista')
    else:
        form = CrearUsuarioForm()

    return render(request, 'core/usuario_form.html', {
        'form': form,
        'modo': 'crear',
    })


@permiso_modulo_requerido('usuarios')
def usuarios_editar(request, user_id):
    usuario_editado = get_object_or_404(User, pk=user_id)

    if request.method == 'POST':
        form = EditarUsuarioForm(request.POST, instance=usuario_editado)

        if form.is_valid():
            nuevo_grupo = form.cleaned_data['grupo']

            # No permitir que el último administrador pierda su rol.
            if es_ultimo_administrador(usuario_editado) and nuevo_grupo.name != 'Administrador':
                messages.error(
                    request,
                    'No puedes quitar el rol de Administrador al último '
                    'administrador del sistema.'
                )
                return redirect('usuarios_editar', user_id=usuario_editado.pk)

            form.save()
            usuario_editado.groups.set([nuevo_grupo])
            messages.success(request, 'Usuario actualizado correctamente.')
            return redirect('usuarios_lista')
    else:
        rol_actual = usuario_editado.groups.first()
        form = EditarUsuarioForm(
            instance=usuario_editado,
            initial={'grupo': rol_actual.pk if rol_actual else None},
        )

    return render(request, 'core/usuario_form.html', {
        'form': form,
        'modo': 'editar',
        'usuario_editado': usuario_editado,
    })


@permiso_modulo_requerido('usuarios')
def usuarios_cambiar_password(request, user_id):
    usuario_editado = get_object_or_404(User, pk=user_id)

    if request.method == 'POST':
        form = AdminPasswordChangeForm(usuario_editado, request.POST)

        if form.is_valid():
            form.save()
            messages.success(request, 'Contraseña actualizada correctamente.')
            return redirect('usuarios_lista')
    else:
        form = AdminPasswordChangeForm(usuario_editado)

    return render(request, 'core/usuario_password.html', {
        'form': form,
        'usuario_editado': usuario_editado,
    })


@permiso_modulo_requerido('usuarios')
@require_POST
def usuarios_desactivar(request, user_id):
    usuario_editado = get_object_or_404(User, pk=user_id)

    if usuario_editado.pk == request.user.pk:
        messages.error(request, 'No puedes desactivarte a ti mismo.')
        return redirect('usuarios_lista')

    if es_ultimo_administrador(usuario_editado):
        messages.error(
            request,
            'No puedes desactivar al último administrador del sistema.'
        )
        return redirect('usuarios_lista')

    usuario_editado.is_active = False
    usuario_editado.save()
    messages.success(request, f'Usuario {usuario_editado.username} desactivado.')
    return redirect('usuarios_lista')


@permiso_modulo_requerido('usuarios')
@require_POST
def usuarios_activar(request, user_id):
    usuario_editado = get_object_or_404(User, pk=user_id)
    usuario_editado.is_active = True
    usuario_editado.save()
    messages.success(request, f'Usuario {usuario_editado.username} activado.')
    return redirect('usuarios_lista')


# ============================================================
# ROLES Y PERMISOS (Fase 2, protección de acceso migrada en Fase 3)
#
# Vista dentro del módulo Usuarios. Usa exclusivamente
# django.contrib.auth.models.Group/Permission y el decorador nativo
# permission_required('core.gestionar_roles', raise_exception=True).
# Desde la Fase 3, las vistas de Usuarios (arriba) también usan
# has_perm('core.ver_usuarios') a través de @permiso_modulo_requerido,
# así que todo el módulo Usuarios (lista + roles) queda unificado bajo
# el mismo sistema de permisos dinámicos.
# ============================================================

def _rol_protegido(grupo):
    """El único rol especial del sistema, Administrador, no se puede
    eliminar ni renombrar desde esta interfaz, para no romper
    es_administrador() (core/permisos.py), que compara por nombre de
    grupo. Esa función no forma parte del control de acceso a páginas
    (eso lo resuelve Django Permission + has_perm): resuelve una regla
    de negocio distinta, la protección contra quedarse sin
    administradores, por eso se mantiene aparte."""
    return grupo.name in GRUPOS_DEL_SISTEMA


@login_required
@permission_required('core.gestionar_roles', raise_exception=True)
def roles_lista(request):
    roles = Group.objects.all().order_by('name')

    rol_id = request.GET.get('rol')
    rol_seleccionado = roles.filter(pk=rol_id).first() if rol_id else None
    if rol_seleccionado is None:
        rol_seleccionado = roles.first()

    matriz_permisos = []
    if rol_seleccionado is not None:
        ids_actuales = rol_seleccionado.permissions.filter(
            content_type__app_label='core'
        ).values_list('id', flat=True)
        matriz_permisos = construir_matriz_permisos(ids_actuales)

    return render(request, 'core/roles_permisos.html', {
        'roles': [
            {
                'objeto': r,
                'protegido': _rol_protegido(r),
                'cantidad_usuarios': r.user_set.count(),
                'seleccionado': rol_seleccionado is not None and r.pk == rol_seleccionado.pk,
            }
            for r in roles
        ],
        'rol_seleccionado': rol_seleccionado,
        'rol_protegido': _rol_protegido(rol_seleccionado) if rol_seleccionado else False,
        'matriz_permisos': matriz_permisos,
        'puede_gestionar_recuperaciones': request.user.has_perm('core.gestionar_recuperaciones'),
    })


@login_required
@permission_required('core.gestionar_roles', raise_exception=True)
def roles_crear(request):
    if request.method == 'POST':
        form = RolForm(request.POST)
        if form.is_valid():
            nuevo_rol = form.save()
            permisos = permisos_validos_desde_ids(request.POST.getlist('permisos'))
            nuevo_rol.permissions.set(permisos)
            messages.success(request, f'Rol "{nuevo_rol.name}" creado correctamente.')
            return redirect(f"{reverse('roles_lista')}?rol={nuevo_rol.pk}")
    else:
        form = RolForm()

    return render(request, 'core/rol_form.html', {
        'form': form,
        'matriz_permisos': construir_matriz_permisos(),
    })


@login_required
@permission_required('core.gestionar_roles', raise_exception=True)
@require_POST
def roles_actualizar(request, group_id):
    rol = get_object_or_404(Group, pk=group_id)
    protegido = _rol_protegido(rol)

    if not protegido:
        nuevo_nombre = request.POST.get('nombre', '').strip()
        if not nuevo_nombre:
            messages.error(request, 'El nombre del rol no puede estar vacío.')
            return redirect(f"{reverse('roles_lista')}?rol={rol.pk}")
        if Group.objects.exclude(pk=rol.pk).filter(name=nuevo_nombre).exists():
            messages.error(request, f'Ya existe un rol llamado "{nuevo_nombre}".')
            return redirect(f"{reverse('roles_lista')}?rol={rol.pk}")
        rol.name = nuevo_nombre
        rol.save()
    # Si el rol está protegido, se ignora cualquier intento de renombrarlo
    # aunque llegue en el POST: la protección es del backend, no solo
    # de que el campo esté deshabilitado en el HTML.

    permisos = permisos_validos_desde_ids(request.POST.getlist('permisos'))

    if protegido:
        faltantes = permisos_criticos_faltantes([p.pk for p in permisos])
        if faltantes:
            messages.error(
                request,
                f'No puedes guardar: el rol "{rol.name}" no puede quedarse sin '
                f'estos permisos críticos para administrar el sistema: '
                f'{", ".join(sorted(faltantes))}.'
            )
            return redirect(f"{reverse('roles_lista')}?rol={rol.pk}")

    rol.permissions.set(permisos)

    messages.success(request, f'Rol "{rol.name}" actualizado correctamente.')
    return redirect(f"{reverse('roles_lista')}?rol={rol.pk}")


@login_required
@permission_required('core.gestionar_roles', raise_exception=True)
@require_POST
def roles_eliminar(request, group_id):
    rol = get_object_or_404(Group, pk=group_id)

    if _rol_protegido(rol):
        messages.error(
            request,
            f'"{rol.name}" es el rol protegido del sistema y no puede eliminarse.'
        )
        return redirect('roles_lista')

    cantidad_usuarios = rol.user_set.count()
    if cantidad_usuarios > 0:
        messages.error(
            request,
            f'No puedes eliminar "{rol.name}" porque tiene {cantidad_usuarios} '
            'usuario(s) asignado(s). Reasigna esos usuarios a otro rol primero.'
        )
        return redirect('roles_lista')

    nombre = rol.name
    rol.delete()
    messages.success(request, f'Rol "{nombre}" eliminado.')
    return redirect('roles_lista')


# ============================================================
# RECUPERACIÓN DE CONTRASEÑAS (Fase 4)
#
# Vista dentro del módulo Usuarios, protegida exclusivamente con
# core.gestionar_recuperaciones (permiso nuevo, Fase 4), igual patrón
# que gestionar_roles en Fase 2. El administrador NUNCA ve ni define
# la contraseña del empleado: solo autoriza/rechaza la solicitud. El
# propio usuario establece su nueva contraseña con
# django.contrib.auth.forms.SetPasswordForm (hashing nativo de
# Django, set_password() internamente).
#
# No se usa correo/SMTP: al autorizar, el enlace de un solo uso se
# muestra al administrador (vía mensaje) para que lo comparta con el
# empleado por el medio que la empresa prefiera.
# ============================================================

def _buscar_usuario_por_identificador(identificador):
    """Busca por username o email, sin distinguir mayúsculas/minúsculas.
    Devuelve None si no hay coincidencia o si el usuario está inactivo,
    sin dar ninguna pista adicional a quien llama (ver
    recuperacion_solicitar, que siempre responde igual)."""
    identificador = identificador.strip()
    if not identificador:
        return None
    usuario = User.objects.filter(username__iexact=identificador, is_active=True).first()
    if usuario is None and '@' in identificador:
        usuario = User.objects.filter(email__iexact=identificador, is_active=True).first()
    return usuario


def recuperacion_solicitar(request):
    """
    Vista pública (sin login): "¿Olvidaste tu contraseña?" desde el
    Login. Registra una SolicitudRecuperacionPassword en estado
    Pendiente si el identificador corresponde a una cuenta activa y
    esa cuenta no tiene ya una solicitud Pendiente.

    Por seguridad (evitar enumeración de usuarios), la respuesta que
    ve la persona es SIEMPRE el mismo mensaje genérico, exista o no
    la cuenta, y haya o no una solicitud pendiente previa. La lógica
    de "no crear duplicados" sí se aplica en el backend, solo que no
    se refleja con un mensaje distinto en esta pantalla pública.
    """
    if request.method == 'POST':
        form = SolicitarRecuperacionForm(request.POST)
        if form.is_valid():
            usuario = _buscar_usuario_por_identificador(form.cleaned_data['identificador'])
            if usuario is not None:
                ya_pendiente = SolicitudRecuperacionPassword.objects.filter(
                    usuario=usuario, estado=SolicitudRecuperacionPassword.Estado.PENDIENTE,
                ).exists()
                if not ya_pendiente:
                    SolicitudRecuperacionPassword.objects.create(usuario=usuario)
            messages.success(
                request,
                'Si la solicitud puede procesarse, será revisada por el administrador.'
            )
            return redirect('recuperacion_solicitar')
    else:
        form = SolicitarRecuperacionForm()

    return render(request, 'core/recuperacion_solicitar.html', {'form': form})


@login_required
@permission_required('core.gestionar_recuperaciones', raise_exception=True)
def recuperacion_lista(request):
    todas = SolicitudRecuperacionPassword.objects.select_related(
        'usuario', 'administrador_resolutor'
    )

    conteos = {
        'Pendiente': todas.filter(estado='Pendiente').count(),
        'Aprobada': todas.filter(estado='Aprobada').count(),
        'Rechazada': todas.filter(estado='Rechazada').count(),
        'Completada': todas.filter(estado='Completada').count(),
    }

    filtro = request.GET.get('estado', '')
    solicitudes = todas.filter(estado=filtro) if filtro in conteos else todas

    frecuencia = (
        todas.values('usuario__id', 'usuario__username')
        .annotate(total=models.Count('id'), ultima=models.Max('fecha_solicitud'))
        .order_by('-total')
    )

    return render(request, 'core/recuperacion_lista.html', {
        'conteos': conteos,
        'filtro_actual': filtro,
        'solicitudes': solicitudes,
        'frecuencia': frecuencia,
        'puede_gestionar_roles': request.user.has_perm('core.gestionar_roles'),
    })


@login_required
@permission_required('core.gestionar_recuperaciones', raise_exception=True)
@require_POST
def recuperacion_autorizar(request, solicitud_id):
    solicitud = get_object_or_404(SolicitudRecuperacionPassword, pk=solicitud_id)

    if solicitud.estado != SolicitudRecuperacionPassword.Estado.PENDIENTE:
        messages.error(request, 'Esa solicitud ya fue resuelta anteriormente.')
        return redirect('recuperacion_lista')

    solicitud.estado = SolicitudRecuperacionPassword.Estado.APROBADA
    solicitud.fecha_resolucion = now()
    solicitud.administrador_resolutor = request.user
    solicitud.save(update_fields=['estado', 'fecha_resolucion', 'administrador_resolutor'])

    token = generar_autorizacion(solicitud)
    enlace = request.build_absolute_uri(
        reverse('recuperacion_establecer_password', args=[solicitud.pk, token])
    )
    messages.success(
        request,
        f'Solicitud de {solicitud.usuario.username} autorizada. Comparte este enlace de '
        f'un solo uso con la persona (vence en 24 horas): {enlace}'
    )
    return redirect('recuperacion_lista')


@login_required
@permission_required('core.gestionar_recuperaciones', raise_exception=True)
@require_POST
def recuperacion_rechazar(request, solicitud_id):
    solicitud = get_object_or_404(SolicitudRecuperacionPassword, pk=solicitud_id)

    if solicitud.estado != SolicitudRecuperacionPassword.Estado.PENDIENTE:
        messages.error(request, 'Esa solicitud ya fue resuelta anteriormente.')
        return redirect('recuperacion_lista')

    solicitud.estado = SolicitudRecuperacionPassword.Estado.RECHAZADA
    solicitud.fecha_resolucion = now()
    solicitud.administrador_resolutor = request.user
    solicitud.save(update_fields=['estado', 'fecha_resolucion', 'administrador_resolutor'])
    invalidar_autorizacion(solicitud)  # defensivo: nunca debería haber token aquí

    messages.success(request, f'Solicitud de {solicitud.usuario.username} rechazada.')
    return redirect('recuperacion_lista')


def recuperacion_establecer_password(request, solicitud_id, token):
    """
    Vista pública (sin login, porque el usuario justamente no puede
    entrar): confirma el enlace de un solo uso y permite establecer
    la nueva contraseña. Usa SetPasswordForm (hashing nativo de
    Django), nunca recibe ni guarda la contraseña en texto plano.
    """
    solicitud = get_object_or_404(SolicitudRecuperacionPassword, pk=solicitud_id)

    if not autorizacion_valida(solicitud, token):
        return render(request, 'core/recuperacion_enlace_invalido.html', status=403)

    if request.method == 'POST':
        form = SetPasswordForm(user=solicitud.usuario, data=request.POST)
        if form.is_valid():
            form.save()  # set_password() + save() internamente
            solicitud.estado = SolicitudRecuperacionPassword.Estado.COMPLETADA
            solicitud.fecha_completada = now()
            solicitud.save(update_fields=['estado', 'fecha_completada'])
            invalidar_autorizacion(solicitud)
            messages.success(request, 'Tu contraseña fue actualizada. Ya puedes iniciar sesión.')
            return redirect('login')
    else:
        form = SetPasswordForm(user=solicitud.usuario)

    return render(request, 'core/recuperacion_establecer_password.html', {
        'form': form,
        'solicitud': solicitud,
    })

# ============================================================
# COMPRAS (Fase 5)
#
# Proveedor -> Orden de compra -> Recepción -> Inventario -> Compra -> Finanzas
#
# Acceso de VISTA a todo el módulo: core.ver_compras (403 duro, mismo
# patrón que permiso_modulo_requerido de core/permisos_django.py).
# Acciones que escriben datos: además requieren el permiso específico
# del submódulo (gestionar_proveedores / gestionar_ordenes_compra /
# gestionar_recepciones / gestionar_compras), comprobado en backend,
# nunca solo ocultando botones.
#
# Reutiliza Producto (con tipo_articulo), MovimientoStock (recepciones)
# y MovimientoFinanciero (compras al contado). No crea sistemas
# paralelos de inventario ni de finanzas.
# ============================================================

@login_required
@permission_required('core.ver_compras', raise_exception=True)
def compras_proveedores(request):
    puede_gestionar = request.user.has_perm('core.gestionar_proveedores')

    if request.method == 'POST':
        if not puede_gestionar:
            raise PermissionDenied('No tienes permiso para gestionar proveedores.')
        form = ProveedorForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Proveedor creado correctamente.')
            return redirect('compras_proveedores')
    else:
        form = ProveedorForm()

    return render(request, 'core/compras_proveedores.html', {
        'form': form,
        'proveedores': Proveedor.objects.all(),
        'puede_gestionar': puede_gestionar,
    })


@login_required
@permission_required('core.gestionar_proveedores', raise_exception=True)
def compras_proveedor_editar(request, proveedor_id):
    proveedor = get_object_or_404(Proveedor, pk=proveedor_id)

    if request.method == 'POST':
        form = ProveedorForm(request.POST, instance=proveedor)
        if form.is_valid():
            form.save()
            messages.success(request, f'Proveedor "{proveedor.nombre}" actualizado.')
            return redirect('compras_proveedores')
    else:
        form = ProveedorForm(instance=proveedor)

    return render(request, 'core/compras_proveedor_form.html', {
        'form': form,
        'proveedor': proveedor,
    })


@login_required
@permission_required('core.gestionar_proveedores', raise_exception=True)
@require_POST
def compras_proveedor_toggle(request, proveedor_id):
    proveedor = get_object_or_404(Proveedor, pk=proveedor_id)
    proveedor.activo = not proveedor.activo
    proveedor.save(update_fields=['activo'])
    estado = 'activado' if proveedor.activo else 'desactivado'
    messages.success(request, f'Proveedor "{proveedor.nombre}" {estado}.')
    return redirect('compras_proveedores')


@login_required
@permission_required('core.ver_compras', raise_exception=True)
def compras_ordenes(request):
    money_field = models.DecimalField(max_digits=18, decimal_places=2)
    ordenes = (
        OrdenCompra.objects.select_related('proveedor')
        .annotate(
            total_estimado_calc=Coalesce(
                models.Sum(
                    models.ExpressionWrapper(
                        models.F('detalles__cantidad_solicitada') * models.F('detalles__costo_unitario'),
                        output_field=money_field,
                    )
                ),
                models.Value(Decimal('0'), output_field=money_field),
                output_field=money_field,
            )
        )
    )
    return render(request, 'core/compras_ordenes.html', {
        'ordenes': ordenes,
        'puede_gestionar': request.user.has_perm('core.gestionar_ordenes_compra'),
    })


@login_required
@permission_required('core.gestionar_ordenes_compra', raise_exception=True)
def compras_orden_nueva(request):
    if request.method == 'POST':
        form = OrdenCompraForm(request.POST)
        formset = DetalleOrdenCompraFormSet(request.POST, instance=OrdenCompra())
        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                orden = form.save(commit=False)
                orden.creado_por = request.user
                orden.save()
                formset.instance = orden
                formset.save()
            messages.success(request, f'Orden de compra #{orden.pk} creada en Borrador.')
            return redirect('compras_orden_detalle', orden_id=orden.pk)
    else:
        form = OrdenCompraForm()
        formset = DetalleOrdenCompraFormSet(instance=OrdenCompra())

    return render(request, 'core/compras_orden_form.html', {
        'form': form,
        'formset': formset,
        # Solo para sugerir un costo de partida en el formulario (el
        # campo sigue siendo editable): último costo conocido por
        # producto. No cambia qué se guarda ni la validación.
        'catalogo_costos_json': [
            {'id': p.id, 'precio': float(p.costo_compra)} for p in Producto.objects.all()
        ],
    })


@login_required
@permission_required('core.ver_compras', raise_exception=True)
def compras_orden_detalle(request, orden_id):
    orden = get_object_or_404(OrdenCompra.objects.select_related('proveedor'), pk=orden_id)
    detalles = _detalles_orden_optimizados().filter(orden=orden)
    return render(request, 'core/compras_orden_detalle.html', {
        'orden': orden,
        'detalles': detalles,
        # Solo para mostrarlo en pantalla (OrdenCompra no guarda un
        # total propio, es una solicitud, no una operación
        # financiera): suma de las líneas ya consultadas arriba.
        'total_estimado': sum((d.subtotal for d in detalles), Decimal('0')),
        'recepciones': orden.recepciones.select_related('recibido_por').all(),
        'puede_gestionar_ordenes': request.user.has_perm('core.gestionar_ordenes_compra'),
        'puede_gestionar_recepciones': request.user.has_perm('core.gestionar_recepciones'),
    })


@login_required
@permission_required('core.gestionar_ordenes_compra', raise_exception=True)
@require_POST
def compras_orden_enviar(request, orden_id):
    orden = get_object_or_404(OrdenCompra, pk=orden_id)
    if orden.estado != 'Borrador':
        messages.error(request, 'Solo una orden en Borrador puede enviarse.')
    else:
        orden.estado = 'Enviada'
        orden.save(update_fields=['estado'])
        messages.success(request, f'Orden #{orden.pk} enviada al proveedor.')
    return redirect('compras_orden_detalle', orden_id=orden.pk)


@login_required
@permission_required('core.gestionar_ordenes_compra', raise_exception=True)
@require_POST
def compras_orden_cancelar(request, orden_id):
    orden = get_object_or_404(OrdenCompra, pk=orden_id)
    if orden.estado in ('Recibida', 'Cancelada'):
        messages.error(request, 'Esa orden ya no puede cancelarse.')
    else:
        orden.estado = 'Cancelada'
        orden.save(update_fields=['estado'])
        messages.success(request, f'Orden #{orden.pk} cancelada.')
    return redirect('compras_orden_detalle', orden_id=orden.pk)


@login_required
@permission_required('core.gestionar_recepciones', raise_exception=True)
def compras_recepcion_nueva(request, orden_id):
    orden = get_object_or_404(OrdenCompra, pk=orden_id)
    detalles_pendientes = [
        d for d in _detalles_orden_optimizados().filter(orden=orden)
        if d.cantidad_pendiente > 0
    ]

    if orden.estado in ('Cancelada', 'Recibida'):
        messages.error(request, 'Esta orden no admite nuevas recepciones.')
        return redirect('compras_orden_detalle', orden_id=orden.pk)

    if request.method == 'POST':
        with transaction.atomic():
            recepcion = Recepcion.objects.create(
                orden=orden, recibido_por=request.user,
                notas=request.POST.get('notas', '').strip(),
            )
            algo_recibido = False
            for detalle in detalles_pendientes:
                campo = f'cantidad_{detalle.pk}'
                valor = request.POST.get(campo, '').strip()
                if not valor:
                    continue
                try:
                    cantidad = int(valor)
                except ValueError:
                    cantidad = 0
                if cantidad <= 0:
                    continue
                cantidad = min(cantidad, detalle.cantidad_pendiente)

                DetalleRecepcion.objects.create(
                    recepcion=recepcion, detalle_orden=detalle, cantidad_recibida=cantidad,
                )
                # Reutiliza exactamente el mismo mecanismo de Inventario
                # (ver views.inventario): actualizar stock + registrar
                # el MovimientoStock, sin crear un sistema paralelo.
                producto = detalle.producto
                producto.stock += cantidad
                producto.save(update_fields=['stock'])
                MovimientoStock.objects.create(
                    producto=producto, tipo='Entrada', cantidad=cantidad,
                    motivo=f'Recepción #{recepcion.pk} - Orden #{orden.pk}',
                )
                algo_recibido = True

            if not algo_recibido:
                recepcion.delete()
                messages.error(request, 'Indica al menos una cantidad recibida mayor a cero.')
                return redirect('compras_recepcion_nueva', orden_id=orden.pk)

            # Comprobar si queda alguna línea pendiente en una sola consulta,
            # en vez de hacer un SUM independiente por cada detalle.
            hay_pendientes = (
                _detalles_orden_optimizados()
                .filter(orden=orden, cantidad_solicitada__gt=models.F('cantidad_recibida_calc'))
                .exists()
            )
            orden.estado = 'Parcialmente recibida' if hay_pendientes else 'Recibida'
            orden.save(update_fields=['estado'])

        messages.success(request, f'Recepción #{recepcion.pk} registrada. Inventario actualizado.')
        return redirect('compras_orden_detalle', orden_id=orden.pk)

    return render(request, 'core/compras_recepcion_form.html', {
        'orden': orden,
        'detalles_pendientes': detalles_pendientes,
    })


@login_required
@permission_required('core.ver_compras', raise_exception=True)
def compras_recepciones(request):
    return render(request, 'core/compras_recepciones.html', {
        'recepciones': Recepcion.objects.select_related('orden', 'orden__proveedor', 'recibido_por').all(),
    })


@login_required
@permission_required('core.ver_compras', raise_exception=True)
def compras_lista(request):
    puede_gestionar = request.user.has_perm('core.gestionar_compras')
    puede_pagar = request.user.has_perm('core.registrar_pagos_compra')

    if request.method == 'POST':
        if not puede_gestionar:
            raise PermissionDenied('No tienes permiso para registrar compras.')
        form = CompraForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                compra = form.save(commit=False)
                compra.creado_por = request.user
                if compra.condicion_pago == 'Contado':
                    compra.monto_pagado = compra.total
                    compra.estado_pago = 'Pagada'
                    compra.fecha_vencimiento = None
                else:
                    compra.monto_pagado = Decimal('0')
                    compra.estado_pago = 'Pendiente'
                compra.save()
                if compra.condicion_pago == 'Contado':
                    MovimientoFinanciero.objects.create(
                        tipo='Gasto', fecha=compra.fecha, categoria='Compras',
                        cliente_proveedor=compra.proveedor.nombre, monto=compra.total,
                        medio_pago='Efectivo', cuenta=_cuenta_automatica_por_medio('Efectivo'),
                        factura=compra.numero_factura, compra=compra,
                    )
            messages.success(request, f'Compra #{compra.pk} registrada.')
            return redirect('compras_detalle', compra_id=compra.pk)
    else:
        form = CompraForm()

    return render(request, 'core/compras_lista.html', {
        'form': form,
        'compras': Compra.objects.select_related('proveedor').all(),
        'puede_gestionar': puede_gestionar,
        'puede_pagar': puede_pagar,
    })


@login_required
@permission_required('core.ver_compras', raise_exception=True)
def compras_detalle(request, compra_id):
    compra = get_object_or_404(Compra.objects.select_related('proveedor'), pk=compra_id)
    return render(request, 'core/compras_detalle.html', {
        'compra': compra,
        'pagos': compra.pagos.select_related('registrado_por', 'movimiento_financiero__cuenta').all(),
        'puede_pagar': request.user.has_perm('core.registrar_pagos_compra'),
        'form': PagoCompraForm(initial={'fecha': now().date()}),
    })


@login_required
@permission_required('core.registrar_pagos_compra', raise_exception=True)
@require_POST
def compras_registrar_pago(request, compra_id):
    form = PagoCompraForm(request.POST)
    if not form.is_valid():
        for errores in form.errors.values():
            for error in errores:
                messages.error(request, error)
        return redirect('compras_detalle', compra_id=compra_id)

    monto = form.cleaned_data['monto']
    try:
        with transaction.atomic():
            compra = Compra.objects.select_for_update().select_related('proveedor').get(pk=compra_id)
            saldo = compra.saldo_pendiente
            if saldo <= 0:
                raise ValueError('Esta compra ya está totalmente pagada.')
            if monto > saldo:
                raise ValueError(
                    f'El monto (RD$ {monto:,.2f}) supera el saldo pendiente '
                    f'(RD$ {saldo:,.2f}). No se registró el pago.'
                )
            pago = form.save(commit=False)
            pago.compra = compra
            pago.registrado_por = request.user
            pago.save()
            compra.monto_pagado = (compra.monto_pagado + monto).quantize(Decimal('0.01'))
            compra.estado_pago = 'Pagada' if compra.monto_pagado >= compra.total else 'Parcial'
            compra.save(update_fields=['monto_pagado', 'estado_pago'])

            cuenta_financiera = form.cleaned_data.get('cuenta_financiera') or _cuenta_automatica_por_medio(pago.metodo_pago)
            MovimientoFinanciero.objects.create(
                tipo='Gasto', fecha=pago.fecha, categoria='Pago Compra',
                cliente_proveedor=compra.proveedor.nombre, monto=pago.monto,
                medio_pago=pago.metodo_pago, cuenta=cuenta_financiera,
                factura=compra.numero_factura, compra=compra, pago_compra=pago,
            )
    except Compra.DoesNotExist:
        messages.error(request, 'La compra no existe.')
        return redirect('compras_lista')
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('compras_detalle', compra_id=compra_id)

    messages.success(request, f'Pago de RD$ {monto:,.2f} registrado para la compra #{compra_id}.')
    return redirect('compras_detalle', compra_id=compra_id)


# ============================================================
# PEDIDOS DE CLIENTES
# ============================================================
# Vive dentro del módulo Clientes (misma navegación con pestañas que
# Compras se presenta como módulo independiente en el sidebar; sus rutas y lógica permanecen aquí.
# Un pedido es una SOLICITUD de un cliente, distinta de una Venta:
# crear o confirmar un pedido no descuenta inventario ni genera una
# Venta automáticamente. Reutiliza Cliente y Producto sin duplicarlos,
# igual que Compras reutiliza Producto para sus órdenes.

TRANSICIONES_PEDIDO_CLIENTE = {
    'Pendiente': {'Confirmado', 'Rechazado', 'Cancelado'},
    'Confirmado': {'Completado', 'Cancelado'},
    'Completado': set(),
    'Rechazado': set(),
    'Cancelado': set(),
}


@login_required
@permission_required('core.ver_pedidos_clientes', raise_exception=True)
def pedidos_clientes_lista(request):
    pedidos = (
        PedidoCliente.objects.select_related('cliente')
        .prefetch_related(
            models.Prefetch('detalles', queryset=_detalles_pedido_optimizados())
        )
    )
    return render(request, 'core/pedidos_clientes_lista.html', {
        'pedidos': pedidos,
        'puede_gestionar': request.user.has_perm('core.gestionar_pedidos_clientes'),
    })


@login_required
@permission_required('core.gestionar_pedidos_clientes', raise_exception=True)
def pedidos_clientes_nuevo(request):
    if request.method == 'POST':
        form = PedidoClienteForm(request.POST)
        formset = DetallePedidoClienteFormSet(request.POST, instance=PedidoCliente())
        if form.is_valid() and formset.is_valid():
            lineas_validas = [
                f for f in formset.forms
                if f.cleaned_data and not f.cleaned_data.get('DELETE')
                and f.cleaned_data.get('producto') and f.cleaned_data.get('cantidad')
            ]
            if not lineas_validas:
                messages.error(request, 'El pedido debe incluir al menos un producto con cantidad.')
            else:
                with transaction.atomic():
                    pedido = form.save(commit=False)
                    pedido.creado_por = request.user
                    pedido.save()
                    formset.instance = pedido
                    formset.save()
                    pedido.total = pedido.total_calculado
                    pedido.save(update_fields=['total'])
                messages.success(request, f'Pedido #{pedido.pk} creado correctamente.')
                return redirect('pedidos_clientes_detalle', pedido_id=pedido.pk)
    else:
        form = PedidoClienteForm()
        formset = DetallePedidoClienteFormSet(instance=PedidoCliente())

    return render(request, 'core/pedidos_clientes_form.html', {
        'form': form,
        'formset': formset,
        'catalogo_precios_json': [
            {'id': p.id, 'precio': float(p.precio_venta)} for p in Producto.objects.all()
        ],
    })


@login_required
@permission_required('core.ver_pedidos_clientes', raise_exception=True)
def pedidos_clientes_detalle(request, pedido_id):
    pedido = get_object_or_404(
        PedidoCliente.objects.select_related('cliente', 'creado_por').prefetch_related(
            models.Prefetch('detalles', queryset=_detalles_pedido_optimizados())
        ),
        pk=pedido_id,
    )
    return render(request, 'core/pedidos_clientes_detalle.html', {
        'pedido': pedido,
        'detalles': pedido.detalles.all(),
        'puede_gestionar': request.user.has_perm('core.gestionar_pedidos_clientes'),
        'transiciones_disponibles': TRANSICIONES_PEDIDO_CLIENTE.get(pedido.estado, set()),
    })


@login_required
@permission_required('core.gestionar_pedidos_clientes', raise_exception=True)
@require_POST
def pedidos_clientes_cambiar_estado(request, pedido_id):
    pedido = get_object_or_404(PedidoCliente, pk=pedido_id)
    nuevo_estado = request.POST.get('estado', '').strip()
    if nuevo_estado not in dict(PedidoCliente.ESTADO_CHOICES):
        messages.error(request, 'Estado inválido.')
        return redirect('pedidos_clientes_detalle', pedido_id=pedido.pk)
    if nuevo_estado not in TRANSICIONES_PEDIDO_CLIENTE.get(pedido.estado, set()):
        messages.error(request, f'No se puede pasar un pedido de "{pedido.estado}" a "{nuevo_estado}".')
        return redirect('pedidos_clientes_detalle', pedido_id=pedido.pk)
    if nuevo_estado == 'Confirmado' and pedido.vencido:
        messages.error(
            request,
            f'El pedido #{pedido.pk} venció el {pedido.fecha_vigencia}. '
            'Crea un nuevo pedido con condiciones vigentes.',
        )
        return redirect('pedidos_clientes_detalle', pedido_id=pedido.pk)
    pedido.estado = nuevo_estado
    pedido.save(update_fields=['estado'])
    messages.success(request, f'Pedido #{pedido.pk} actualizado a "{nuevo_estado}".')
    return redirect('pedidos_clientes_detalle', pedido_id=pedido.pk)


@login_required
@permission_required('core.gestionar_pedidos_clientes', raise_exception=True)
def pedidos_clientes_despachar(request, pedido_id):
    """Despacha total/parcialmente un pedido generando una Venta real."""
    pedido = get_object_or_404(PedidoCliente.objects.select_related('cliente'), pk=pedido_id)
    if pedido.estado != 'Confirmado':
        messages.error(request, 'Solo se pueden despachar pedidos en estado "Confirmado".')
        return redirect('pedidos_clientes_detalle', pedido_id=pedido.pk)
    if not puede_escribir_en(request, 'ventas'):
        messages.error(request, 'No tienes permiso para procesar ventas.')
        return redirect('pedidos_clientes_detalle', pedido_id=pedido.pk)

    detalles_pendientes = [
        d for d in _detalles_pedido_optimizados().filter(pedido=pedido)
        if d.cantidad_pendiente > 0
    ]

    if request.method == 'POST':
        try:
            with transaction.atomic():
                pedido_bloqueado = PedidoCliente.objects.select_for_update().select_related('cliente').get(pk=pedido.pk)
                if pedido_bloqueado.estado != 'Confirmado':
                    raise ValueError('El estado del pedido cambió y ya no puede despacharse.')
                detalles_bloqueados = {
                    d.pk: d for d in (
                        DetallePedidoCliente.objects.select_for_update()
                        .select_related('producto')
                        .filter(pedido=pedido_bloqueado)
                    )
                }
                facturado_por_detalle = {
                    fila['detalle_pedido_id']: fila['total'] or 0
                    for fila in (
                        DetalleVenta.objects
                        .filter(detalle_pedido_id__in=detalles_bloqueados)
                        .values('detalle_pedido_id')
                        .annotate(total=models.Sum('cantidad'))
                    )
                }
                for detalle_bloqueado in detalles_bloqueados.values():
                    detalle_bloqueado.cantidad_facturada_calc = facturado_por_detalle.get(
                        detalle_bloqueado.pk, 0
                    )

                cliente = pedido_bloqueado.cliente
                datos_empresa = DatosEmpresa.obtener()
                tasa_empresa = Decimal(str(datos_empresa.itbs_porcentaje))
                lineas = []
                subtotal_bruto = Decimal('0')
                subtotal = Decimal('0')
                itbs = Decimal('0')

                for detalle in detalles_pendientes:
                    actual = detalles_bloqueados.get(detalle.pk)
                    if actual is None:
                        continue
                    valor = request.POST.get(f'cantidad_{detalle.pk}', '').strip()
                    if not valor:
                        continue
                    try:
                        cantidad = int(valor)
                    except ValueError:
                        cantidad = 0
                    if cantidad <= 0:
                        continue
                    pendiente = actual.cantidad_pendiente
                    if cantidad > pendiente:
                        raise ValueError(
                            f'La cantidad de {actual.producto.nombre} supera lo pendiente ({pendiente}).'
                        )
                    producto = Producto.objects.select_for_update().get(pk=actual.producto_id)
                    if cantidad > producto.stock:
                        raise ValueError(
                            f'Stock insuficiente para {producto.nombre}. Disponible: {producto.stock}'
                        )
                    precio = actual.precio_unitario
                    calculo = _calcular_linea_fiscal(
                        producto, cantidad, precio, Decimal('0'), cliente, tasa_empresa
                    )
                    lineas.append((producto, actual, cantidad, precio, calculo))
                    subtotal_bruto += calculo['importe_bruto']
                    subtotal += calculo['subtotal']
                    itbs += calculo['itbs']

                if not lineas:
                    raise ValueError('Indica al menos una cantidad a despachar.')

                condicion_raw = _normalizar_texto(request.POST.get('condicion_pago') or 'Contado')
                condicion = 'Credito' if condicion_raw == 'credito' else 'Contado'
                metodo_raw = _normalizar_texto(request.POST.get('metodo_pago') or 'Efectivo')
                metodo = 'Tarjeta' if metodo_raw == 'tarjeta' else 'Efectivo'
                tipo_comprobante = _resolver_tipo_comprobante(cliente, request.POST.get('tipo_comprobante'))
                total = (subtotal + itbs).quantize(Decimal('0.01'))

                venta = Venta.objects.create(
                    cliente=cliente,
                    pedido=pedido_bloqueado,
                    subtotal_bruto=subtotal_bruto,
                    descuento=Decimal('0'),
                    subtotal=subtotal,
                    itbs=itbs,
                    total=total,
                    metodo_pago=metodo,
                    condicion_pago=condicion,
                    tipo_comprobante=tipo_comprobante,
                    vendedor=request.user,
                    observaciones=(request.POST.get('observaciones') or '').strip()[:250],
                    **_snapshot_cliente(cliente),
                )
                for producto, actual, cantidad, precio, calculo in lineas:
                    DetalleVenta.objects.create(
                        venta=venta,
                        producto=producto,
                        detalle_pedido=actual,
                        cantidad=cantidad,
                        precio_unitario=precio,
                        costo_unitario=producto.costo_compra,
                        descuento_porcentaje=Decimal('0'),
                        tasa_itbs=calculo['tasa_itbs'],
                        itbs_monto=calculo['itbs'],
                    )
                    producto.stock -= cantidad
                    producto.save(update_fields=['stock'])
                    MovimientoStock.objects.create(
                        producto=producto,
                        tipo='Salida',
                        cantidad=cantidad,
                        motivo=f'Venta {venta.numero_factura} (Pedido #{pedido_bloqueado.pk})',
                    )

                if condicion == 'Contado':
                    MovimientoFinanciero.objects.create(
                        tipo='Ingreso', fecha=venta.fecha.date(), categoria='Ventas',
                        cliente_proveedor=venta.cliente_nombre, monto=total,
                        medio_pago=metodo, cuenta=_cuenta_automatica_por_medio(metodo),
                        factura=venta.numero_factura, venta=venta,
                    )
                else:
                    CuentaPorCobrar.objects.create(
                        venta=venta, cliente=cliente, importe_original=total,
                        fecha_emision=venta.fecha.date(),
                        fecha_vencimiento=venta.fecha.date() + timedelta(days=datos_empresa.dias_credito),
                        creado_por=request.user,
                    )

                # Una sola consulta comprueba si todavía queda alguna línea
                # por facturar después de crear la venta.
                hay_pendientes = (
                    _detalles_pedido_optimizados()
                    .filter(pedido=pedido_bloqueado, cantidad__gt=models.F('cantidad_facturada_calc'))
                    .exists()
                )
                if not hay_pendientes:
                    pedido_bloqueado.estado = 'Completado'
                    pedido_bloqueado.save(update_fields=['estado'])
        except (ValueError, PedidoCliente.DoesNotExist) as exc:
            messages.error(request, str(exc))
            return redirect('pedidos_clientes_despachar', pedido_id=pedido.pk)

        messages.success(
            request,
            f'{venta.numero_factura} generada desde el Pedido #{pedido.pk}. '
            + ('El saldo quedó en Cuentas por Cobrar.' if condicion == 'Credito' else '')
        )
        return redirect('venta_factura', venta_id=venta.id)

    return render(request, 'core/pedidos_clientes_despacho_form.html', {
        'pedido': pedido,
        'detalles_pendientes': detalles_pendientes,
    })

