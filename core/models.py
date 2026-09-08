from decimal import Decimal
from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator

class Producto(models.Model):
    CATEGORIA_CHOICES = [
        ('Laptops', 'Laptops'),
        ('Escritorio', 'Escritorio'),
        ('Perifericos', 'Perifericos'),
        ('Impresion', 'Impresion'),
        ('Almacenamiento', 'Almacenamiento'),
        ('Audio y Video', 'Audio y Video'),
        ('Mobiliario', 'Mobiliario'),
    ]
    TIPO_ARTICULO_CHOICES = [
        ('Mercancia', 'Mercancía (para la venta)'),
        ('Material', 'Material o insumo interno'),
    ]

    sku = models.CharField(max_length=10, unique=True, verbose_name="SKU/Cod")
    nombre = models.CharField(max_length=120)
    categoria = models.CharField(max_length=30, choices=CATEGORIA_CHOICES)
    tipo_articulo = models.CharField(
        max_length=15, choices=TIPO_ARTICULO_CHOICES, default='Mercancia',
    )
    costo_compra = models.DecimalField(max_digits=10, decimal_places=2)
    # Precio base de venta, antes de ITBIS. Se conserva un solo precio
    # para evitar inconsistencias entre un precio "con" y otro "sin" impuesto.
    precio_venta = models.DecimalField(max_digits=10, decimal_places=2)
    # Novedad de Rosa: permite que un producto sea fiscalmente exento.
    exento_itbis = models.BooleanField(default=False, verbose_name='Exento de ITBIS')
    stock = models.PositiveIntegerField(default=0)
    stock_minimo = models.PositiveIntegerField(default=5)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.sku} - {self.nombre}"

    @property
    def precio_con_itbis(self):
        """Precio informativo calculado desde el precio base actual.

        La Venta guarda su propio impuesto histórico; esta propiedad es solo
        para mostrar el precio actual del catálogo.
        """
        if self.exento_itbis:
            return self.precio_venta
        try:
            porcentaje = Decimal(str(DatosEmpresa.obtener().itbs_porcentaje)) / Decimal('100')
        except Exception:
            porcentaje = Decimal('0.18')
        return (self.precio_venta * (Decimal('1') + porcentaje)).quantize(Decimal('0.01'))

    @property
    def estado_stock(self):
        if self.stock < self.stock_minimo:
            return "Bajo Stock"
        elif self.stock < self.stock_minimo * 2:
            return "Moderado"
        return "Optimo"

    class Meta:
        ordering = ['sku']



class MovimientoStock(models.Model):
    TIPO_CHOICES = [
        ('Entrada', 'Entrada'),
        ('Salida', 'Salida'),
    ]

    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name='movimientos')
    tipo = models.CharField(max_length=10, choices=TIPO_CHOICES)
    cantidad = models.PositiveIntegerField()
    motivo = models.CharField(max_length=150, blank=True)
    fecha = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.tipo} - {self.producto.nombre} - {self.cantidad}"

    class Meta:
        ordering = ['-fecha']
        indexes = [models.Index(fields=['tipo', '-fecha'], name='movstock_tipo_fecha_idx')]


class MovimientoFinanciero(models.Model):
    TIPO_CHOICES = [
        ('Ingreso', 'Ingreso'),
        ('Gasto', 'Gasto'),
    ]
    MEDIO_PAGO_CHOICES = [
        ('Banco BHD', 'Banco BHD'),
        ('Caja Chica', 'Caja Chica'),
        ('Transferencia', 'Transferencia'),
        ('Efectivo', 'Efectivo'),
        ('Tarjeta', 'Tarjeta'),
    ]

    tipo = models.CharField(max_length=10, choices=TIPO_CHOICES)
    fecha = models.DateField(default=timezone.now)
    categoria = models.CharField(max_length=100)
    cliente_proveedor = models.CharField(max_length=120, verbose_name="Cliente / Proveedor", blank=True)
    monto = models.DecimalField(max_digits=12, decimal_places=2)
    medio_pago = models.CharField(max_length=20, choices=MEDIO_PAGO_CHOICES, blank=True)
    # Novedad de Rosa: identifica dónde está/estuvo el dinero (caja,
    # banco o tarjeta por liquidar) sin perder el campo medio_pago.
    cuenta = models.ForeignKey(
        'CuentaBancaria', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='movimientos',
    )
    factura = models.CharField(max_length=30, blank=True)
    venta = models.ForeignKey(
        'Venta', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='movimientos_financieros',
    )
    compra = models.ForeignKey(
        'Compra', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='movimientos_financieros',
    )
    # Novedades de Mónica: cada abono CxC/CxP queda enlazado de forma
    # 1 a 1 con el movimiento financiero que generó.
    cobro = models.OneToOneField(
        'Cobro', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='movimiento_financiero',
    )
    pago_compra = models.OneToOneField(
        'PagoCompra', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='movimiento_financiero',
    )
    nomina = models.OneToOneField(
        'Nomina', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='movimiento_financiero',
    )

    def __str__(self):
        return f"{self.tipo} - {self.categoria} - RD$ {self.monto}"

    class Meta:
        ordering = ['-fecha']
        indexes = [models.Index(fields=['tipo', '-fecha'], name='movfin_tipo_fecha_idx')]



class Cliente(models.Model):
    TIPO_CHOICES = [
        ('Publico', 'Publico'),
        ('Empresa', 'Empresa'),
        ('Frecuente', 'Frecuente'),
    ]
    CONDICION_FISCAL_CHOICES = [
        ('Normal', 'Normal'),
        ('Exento', 'Exento / Régimen especial'),
    ]

    # Novedad de Mónica: código visible y estable del cliente.
    codigo = models.CharField(max_length=20, unique=True, blank=True)
    nombre = models.CharField(max_length=120)
    empresa = models.CharField(max_length=120, blank=True)
    telefono = models.CharField(max_length=20, blank=True)
    tipo = models.CharField(max_length=15, choices=TIPO_CHOICES, default='Publico')
    rnc_cedula = models.CharField(max_length=20, blank=True, verbose_name='RNC/Cédula')
    email = models.EmailField(blank=True)
    direccion = models.CharField(max_length=200, blank=True)
    # Novedades de Rosa: condición fiscal y desactivación lógica.
    condicion_fiscal = models.CharField(
        max_length=10, choices=CONDICION_FISCAL_CHOICES, default='Normal',
    )
    activo = models.BooleanField(default=True)

    def __str__(self):
        return self.nombre

    def save(self, *args, **kwargs):
        if not self.codigo:
            super().save(*args, **kwargs)
            self.codigo = f'CLI-{self.pk:06d}'
            super().save(update_fields=['codigo'])
            return
        super().save(*args, **kwargs)

    @property
    def ultima_compra(self):
        # Si la vista ya anotó la última compra, reutilizar ese valor evita
        # una consulta adicional por cliente. El fallback conserva exactamente
        # el comportamiento anterior para cualquier otra vista.
        if hasattr(self, 'ultima_compra_calc'):
            return self.ultima_compra_calc
        ultima = self.venta_set.order_by('-fecha').first()
        return ultima.fecha if ultima else None

    class Meta:
        ordering = ['nombre']



class Venta(models.Model):
    METODO_PAGO_CHOICES = [
        ('Efectivo', 'Efectivo'),
        ('Tarjeta', 'Tarjeta'),
    ]
    CONDICION_PAGO_CHOICES = [
        ('Contado', 'Contado'),
        ('Credito', 'Crédito'),
    ]
    TIPO_COMPROBANTE_CHOICES = [
        ('E32', 'E32 - Consumo'),
        ('E31', 'E31 - Crédito Fiscal'),
        ('E44', 'E44 - Regímenes Especiales'),
    ]

    # Número interno de control. Es independiente del tipo de comprobante fiscal.
    numero_factura = models.CharField(max_length=20, unique=True, blank=True)
    cliente = models.ForeignKey(Cliente, on_delete=models.SET_NULL, null=True, blank=True)
    # Snapshot histórico de cliente: una factura no cambia si luego se edita el cliente.
    cliente_nombre = models.CharField(max_length=150, blank=True)
    cliente_rnc_cedula = models.CharField(max_length=20, blank=True)
    cliente_telefono = models.CharField(max_length=20, blank=True)
    cliente_direccion = models.CharField(max_length=200, blank=True)
    cliente_email = models.EmailField(blank=True)
    pedido = models.ForeignKey(
        'PedidoCliente', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ventas',
    )
    fecha = models.DateTimeField(auto_now_add=True)
    subtotal_bruto = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    descuento = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    itbs = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    metodo_pago = models.CharField(max_length=10, choices=METODO_PAGO_CHOICES, default='Efectivo')
    condicion_pago = models.CharField(max_length=10, choices=CONDICION_PAGO_CHOICES, default='Contado')
    # Novedad fiscal de Rosa. No se confunde con condicion_pago.
    tipo_comprobante = models.CharField(
        max_length=3, choices=TIPO_COMPROBANTE_CHOICES, default='E32',
    )
    vendedor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ventas_vendidas',
    )
    observaciones = models.CharField(max_length=250, blank=True)

    @property
    def monto_pagado(self):
        if self.condicion_pago != 'Credito':
            return self.total
        try:
            return min(self.cuenta_por_cobrar.total_cobrado, self.total)
        except (CuentaPorCobrar.DoesNotExist, AttributeError):
            total = self.movimientos_financieros.filter(tipo='Ingreso').aggregate(
                total=models.Sum('monto')
            )['total'] or Decimal('0')
            return min(total, self.total)

    @property
    def saldo_pendiente(self):
        if self.condicion_pago != 'Credito':
            return Decimal('0')
        return max(self.total - self.monto_pagado, Decimal('0'))

    @property
    def estado_pago(self):
        if self.condicion_pago != 'Credito' or self.saldo_pendiente <= 0:
            return 'Pagada'
        if self.monto_pagado > 0:
            return 'Parcial'
        return 'Pendiente'

    def __str__(self):
        return f"{self.numero_factura or ('Venta #' + str(self.pk))} - {self.fecha.strftime('%d-%m-%Y')}"

    def save(self, *args, **kwargs):
        if not self.numero_factura:
            super().save(*args, **kwargs)
            self.numero_factura = f'FAC-{self.pk:06d}'
            super().save(update_fields=['numero_factura'])
            return
        super().save(*args, **kwargs)

    class Meta:
        ordering = ['-fecha']
        indexes = [models.Index(fields=['-fecha'], name='venta_fecha_idx')]



class DetalleVenta(models.Model):
    venta = models.ForeignKey(Venta, on_delete=models.CASCADE, related_name='detalles')
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)
    detalle_pedido = models.ForeignKey(
        'DetallePedidoCliente', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ventas',
    )
    cantidad = models.PositiveIntegerField()
    # Snapshots históricos de precio/costo/tasa: cambiar el Producto en el futuro
    # no cambia la rentabilidad ni los impuestos de una venta ya realizada.
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2)
    costo_unitario = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    descuento_porcentaje = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tasa_itbs = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    itbs_monto = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    @property
    def importe_bruto(self):
        return self.cantidad * self.precio_unitario

    @property
    def descuento_monto(self):
        return (self.importe_bruto * self.descuento_porcentaje / Decimal('100')).quantize(Decimal('0.01'))

    @property
    def subtotal(self):
        return self.importe_bruto - self.descuento_monto

    @property
    def itbs_linea(self):
        return self.itbs_monto

    @property
    def total_linea(self):
        return self.subtotal + self.itbs_monto

    @property
    def costo_total(self):
        return self.cantidad * self.costo_unitario

    @property
    def margen_bruto(self):
        return self.subtotal - self.costo_total

    def __str__(self):
        return f"{self.producto.nombre} x{self.cantidad}"

    class Meta:
        ordering = ['-venta__fecha']



class Empleado(models.Model):
    DEPARTAMENTO_CHOICES = [
        ('Ventas', 'Ventas'),
        ('Finanzas', 'Finanzas'),
        ('Inventario', 'Inventario'),
        ('Admin', 'Admin'),
    ]
    ESTADO_CHOICES = [
        ('Activo', 'Activo'),
        ('Inactivo', 'Inactivo'),
    ]
    TIPO_EMPLEADO_CHOICES = [
        ('Fijo', 'Fijo'),
        ('Por Hora', 'Por Hora'),
    ]
    TIPO_NOMINA_CHOICES = [
        ('Mensual', 'Mensual'),
        ('Quincenal', 'Quincenal'),
    ]
    NIVEL_ACADEMICO_CHOICES = [
        ('Primario', 'Primario'),
        ('Secundario', 'Secundario'),
        ('Tecnico', 'Tecnico'),
        ('Universitario', 'Universitario'),
        ('Postgrado', 'Postgrado'),
    ]

    usuario = models.OneToOneField(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='empleado', verbose_name="Usuario de acceso"
    )
    nombre = models.CharField(max_length=120)
    puesto = models.CharField(max_length=100)
    departamento = models.CharField(max_length=20, choices=DEPARTAMENTO_CHOICES)
    # Novedad de Rosa: permite un área interna más específica sin reemplazar
    # el departamento general que ya usa Chris.
    area = models.CharField(max_length=100, blank=True, verbose_name='Área / Departamento interno')
    telefono = models.CharField(max_length=20, blank=True)
    fecha_ingreso = models.DateField()
    salario = models.DecimalField(max_digits=10, decimal_places=2)
    estado = models.CharField(max_length=10, choices=ESTADO_CHOICES, default='Activo')
    tipo_empleado = models.CharField(max_length=15, choices=TIPO_EMPLEADO_CHOICES, default='Fijo')
    tipo_nomina = models.CharField(max_length=10, choices=TIPO_NOMINA_CHOICES, default='Mensual', verbose_name='Tipo de nómina')
    nivel_academico = models.CharField(max_length=20, choices=NIVEL_ACADEMICO_CHOICES, blank=True)
    horas_extra_mes = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    dias_vacaciones_tomados = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.nombre} - {self.puesto}"

    class Meta:
        ordering = ['nombre']
        indexes = [models.Index(fields=['estado', 'nombre'], name='empleado_estado_nombre_idx')]



class DiaFeriado(models.Model):
    fecha = models.DateField(unique=True)
    nombre = models.CharField(max_length=100)

    def __str__(self):
        return f"{self.nombre} - {self.fecha.strftime('%d-%m-%Y')}"

    class Meta:
        ordering = ['fecha']
        verbose_name = 'Día feriado'
        verbose_name_plural = 'Días feriados'



class Ausencia(models.Model):
    # PROTECT (no CASCADE): decisión de arquitectura confirmada -
    # un Empleado nunca se elimina físicamente desde el CRUD (se
    # desactiva vía estado='Inactivo', ver vista `empleados`), por lo
    # que en el flujo normal esto nunca se dispara. PROTECT queda como
    # segunda capa de seguridad ante un borrado accidental por otra
    # vía (admin, shell, script), evitando perder el historial de
    # ausencias de forma silenciosa. No se introduce ningún sistema de
    # baja lógica nuevo: Empleado.estado ya cumple ese rol.
    empleado = models.ForeignKey(
        Empleado, on_delete=models.PROTECT, related_name='ausencias',
    )
    fecha = models.DateField()
    justificada = models.BooleanField(default=False)
    motivo = models.CharField(max_length=200, blank=True)

    def __str__(self):
        estado = 'Justificada' if self.justificada else 'Injustificada'
        return f"{self.empleado.nombre} - {self.fecha} ({estado})"

    class Meta:
        ordering = ['-fecha']
        indexes = [models.Index(fields=['fecha', 'justificada', 'empleado'], name='ausencia_fecha_estado_idx')]
        verbose_name = 'Ausencia'
        verbose_name_plural = 'Ausencias'



class TramoISR(models.Model):
    """
    Escala progresiva de ISR (DGII) para el cálculo de nómina, como
    tabla editable en vez de constantes hardcodeadas en Python (ver
    decisión de Fase 1: la escala tiene tramos acoplados entre sí, por
    lo que no encaja como campos escalares de DatosEmpresa).

    Fórmula por tramo (aplicada por Fase 3): para una base_anual
    dentro de [limite_inferior, limite_superior) del año
    correspondiente, el ISR anual es monto_acumulado +
    (base_anual - limite_inferior) * tasa / 100.
    `limite_superior = NULL` indica el último tramo (sin tope).

    'anio' (Fase 3): la escala DGII se actualiza por ley de un año a
    otro. Versionar por año permite sembrar la escala de un año nuevo
    sin alterar ni perder la de años anteriores (necesaria para que
    una nómina histórica ya pagada no cambie). unique_together
    (anio, orden) reemplaza el 'orden' único a secas de Fase 1.
    """
    anio = models.PositiveIntegerField(default=2026, verbose_name='Año fiscal')
    orden = models.PositiveSmallIntegerField()
    limite_inferior = models.DecimalField(max_digits=12, decimal_places=2)
    limite_superior = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text='Vacío = sin tope (último tramo).',
    )
    tasa = models.DecimalField(max_digits=5, decimal_places=2, verbose_name='Tasa (%)')
    monto_acumulado = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        verbose_name='ISR acumulado de tramos anteriores',
    )

    def __str__(self):
        limite = f"hasta RD$ {self.limite_superior:,.2f}" if self.limite_superior is not None else "en adelante"
        return f"{self.anio} - Tramo {self.orden}: {limite} ({self.tasa}%)"

    class Meta:
        ordering = ['anio', 'orden']
        verbose_name = 'Tramo de ISR'
        verbose_name_plural = 'Escala de ISR (tramos)'
        constraints = [
            models.UniqueConstraint(fields=['anio', 'orden'], name='unico_tramo_isr_por_anio'),
        ]

class TopeTSS(models.Model):
    """
    Topes de cotización de Seguridad Social (AFP/SFS) por año,
    versionados igual que TramoISR y por la misma razón: la TSS los
    actualiza periódicamente y una nómina histórica no debe cambiar
    si el tope de un año posterior cambia.

    tope_afp / tope_sfs = NULL significa "sin tope aplicado" (se
    cotiza sobre el salario completo). Se deja así -en vez de
    sembrar una cifra- porque el monto exacto vigente es un dato
    legal que debe confirmarse con una fuente oficial antes de
    aplicarse a nóminas reales; no se inventa un número aquí.
    """
    anio = models.PositiveIntegerField(unique=True, verbose_name='Año fiscal')
    tope_afp = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        verbose_name='Tope de cotización AFP (mensual)',
    )
    tope_sfs = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        verbose_name='Tope de cotización SFS (mensual)',
    )

    def __str__(self):
        return f"Topes TSS {self.anio}"

    class Meta:
        ordering = ['-anio']
        verbose_name = 'Tope TSS'
        verbose_name_plural = 'Topes TSS (AFP/SFS) por año'

class Nomina(models.Model):
    """
    Cabecera de una corrida de nómina (Fase 3, mensual exclusivamente
    - decisión definitiva). Snapshot financiero: una vez generada, sus
    DetalleNomina asociados no cambian aunque después cambien
    Empleado.salario, DatosEmpresa o TramoISR/TopeTSS.
    """
    ESTADO_CHOICES = [
        ('Pendiente de pago', 'Pendiente de pago'),
        ('Pagada', 'Pagada'),
    ]

    fecha_inicio = models.DateField()
    fecha_fin = models.DateField()
    fecha_generacion = models.DateTimeField(auto_now_add=True)
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='Pendiente de pago')
    total_bruto = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_deducciones = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_neto = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    generada_por = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='nominas_generadas',
    )
    fecha_pago = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"Nómina {self.fecha_inicio.strftime('%m/%Y')} ({self.estado})"

    @property
    def mes_fiscal(self):
        """(año, mes) del mes que cubre esta nómina. Siempre un mes completo (ver clean())."""
        return (self.fecha_inicio.year, self.fecha_inicio.month)

    def clean(self):
        import calendar
        from django.core.exceptions import ValidationError

        if self.fecha_fin < self.fecha_inicio:
            raise ValidationError('La fecha de fin no puede ser anterior a la fecha de inicio.')
        if self.fecha_inicio.day != 1:
            raise ValidationError('La nómina mensual debe iniciar el primer día del mes.')
        ultimo_dia_mes = calendar.monthrange(self.fecha_inicio.year, self.fecha_inicio.month)[1]
        if self.fecha_fin != self.fecha_inicio.replace(day=ultimo_dia_mes):
            raise ValidationError(
                'La nómina mensual debe terminar el último día del mismo mes de inicio '
                '(no puede abarcar dos meses).'
            )

    class Meta:
        ordering = ['-fecha_inicio']
        verbose_name = 'Nómina'
        verbose_name_plural = 'Nóminas'
        constraints = [
            # La BD impide de raíz generar dos veces la nómina del
            # mismo mes (mismo criterio que Cobro/PagoCompra OneToOne:
            # la integridad no depende de que la vista se acuerde de
            # comprobarlo). fecha_inicio es la clave natural del mes,
            # dado que clean() garantiza que siempre es el día 1.
            models.UniqueConstraint(fields=['fecha_inicio'], name='unico_periodo_de_nomina'),
        ]

class DetalleNomina(models.Model):
    """
    Detalle por empleado de una Nomina. Snapshot histórico completo:
    guarda tanto los insumos (salario_base, dias_ausencia, horas_extra)
    como los resultados (afp, sfs, isr, salario_neto...) ya calculados
    en el momento de generar la nómina, para que el recibo y el PDF
    nunca tengan que recalcular una nómina pasada.
    """
    nomina = models.ForeignKey(Nomina, on_delete=models.CASCADE, related_name='detalles')
    # PROTECT (no CASCADE): mismo criterio que Ausencia.empleado -
    # un Empleado nunca se elimina físicamente, así que esto nunca se
    # dispara en el flujo normal; es una segunda capa de seguridad.
    empleado = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='detalles_nomina')
    # Snapshot del nombre del empleado en el momento del cálculo: si
    # el empleado se desactiva o se le corrige el nombre después, el
    # recibo histórico no cambia.
    empleado_nombre = models.CharField(max_length=120)

    salario_base = models.DecimalField(max_digits=12, decimal_places=2)
    tipo_nomina = models.CharField(max_length=10, default='Mensual')
    primera_quincena = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    segunda_quincena = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    dias_ausencia = models.PositiveIntegerField(default=0)
    horas_extra = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    pago_horas_extra = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    afp = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sfs = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    isr = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    descuento_ausencias = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    salario_bruto = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_deducciones = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    salario_neto = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def __str__(self):
        return f"{self.empleado_nombre} - Nómina #{self.nomina_id}"

    class Meta:
        ordering = ['empleado_nombre']
        verbose_name = 'Detalle de nómina'
        verbose_name_plural = 'Detalles de nómina'
        constraints = [
            models.UniqueConstraint(fields=['nomina', 'empleado'], name='unico_empleado_por_nomina'),
        ]

class ReporteGenerado(models.Model):
    TIPO_CHOICES = [
        ('Financiero', 'Financiero'),
        ('Ventas', 'Ventas'),
        ('Inventario', 'Inventario'),
        ('RRHH', 'RRHH'),
        ('Impuestos', 'Impuestos'),
    ]
    FORMATO_CHOICES = [
        ('PDF', 'PDF'),
        ('Excel', 'Excel'),
    ]

    nombre = models.CharField(max_length=150)
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    fecha_desde = models.DateField()
    fecha_hasta = models.DateField()
    fecha_generacion = models.DateTimeField(auto_now_add=True)
    formato = models.CharField(max_length=10, choices=FORMATO_CHOICES, default='PDF')
    archivo = models.FileField(upload_to='reportes/')

    def __str__(self):
        return self.nombre

    class Meta:
        ordering = ['-fecha_generacion']


class RegistroActividad(models.Model):
    # "Reiniciar Ventas del Mes" se retiro del catalogo: la
    # funcionalidad que generaba esta accion (resetear_ventas_mes) fue
    # eliminada del proyecto porque las ventas son historial permanente
    # y no deben poder "reiniciarse". Ver commit de limpieza.
    ACCION_CHOICES = [
        ('Eliminar Cuenta', 'Eliminar Cuenta'),
    ]

    empleado = models.ForeignKey(Empleado, on_delete=models.SET_NULL, null=True, blank=True)
    usuario_username = models.CharField(max_length=150)
    accion = models.CharField(max_length=50, choices=ACCION_CHOICES)
    detalle = models.TextField(blank=True)
    fecha = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.accion} - {self.usuario_username} - {self.fecha.strftime('%d-%m-%Y %H:%M')}"

    class Meta:
        ordering = ['-fecha']


class DatosEmpresa(models.Model):
    MONEDA_CHOICES = [
        ('RD$ - Peso Dominicano', 'RD$ - Peso Dominicano'),
        ('US$ - Dolar', 'US$ - Dolar'),
    ]

    nombre_comercial = models.CharField(max_length=150, default='Cromf Finanzas')
    rnc = models.CharField(max_length=20, blank=True)
    direccion = models.CharField(max_length=200, blank=True)
    telefono = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    moneda = models.CharField(max_length=30, choices=MONEDA_CHOICES, default='RD$ - Peso Dominicano')
    itbs_porcentaje = models.DecimalField(max_digits=5, decimal_places=2, default=18.00)
    isr_ano_anterior = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Política comercial de crédito para CxC. Se congela en cada cuenta al emitirla.
    dias_credito = models.PositiveIntegerField(default=30, verbose_name='Plazo de crédito (días)')
    afp_porcentaje = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('2.87'), verbose_name='AFP (%)')
    sfs_porcentaje = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('3.04'), verbose_name='SFS (%)')
    dias_mes_calculo = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('23.83'), verbose_name='Días de referencia para cálculo diario/hora extra')
    factor_hora_extra = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal('1.35'), verbose_name='Factor de pago de hora extra')
    # Integrado desde rosaFinal: aporte patronal SRL configurable.
    porcentaje_riesgo_laboral = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal('1.10'),
        validators=[MinValueValidator(Decimal('1.10')), MaxValueValidator(Decimal('1.40'))],
        verbose_name='Riesgo Laboral SRL (%)',
    )

    def __str__(self):
        return self.nombre_comercial

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def obtener(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    class Meta:
        verbose_name = "Datos de la Empresa"
        verbose_name_plural = "Datos de la Empresa"



class PreferenciasNotificacion(models.Model):
    usuario = models.OneToOneField(User, on_delete=models.CASCADE, related_name='preferencias_notificacion')
    alertas_stock_bajo = models.BooleanField(default=True)
    recomendaciones_ia = models.BooleanField(default=True)
    resumen_diario_correo = models.BooleanField(default=False)
    nuevas_ventas = models.BooleanField(default=True)

    def __str__(self):
        return f"Preferencias de {self.usuario.username}"


class ConsultaIA(models.Model):
    """Historial de preguntas realizadas desde el módulo Modo IA.

    La respuesta se guarda para que cada usuario pueda consultar su propio
    historial. No reemplaza los datos del negocio ni modifica Ventas,
    Inventario o Finanzas.
    """

    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='consultas_ia')
    pregunta = models.TextField()
    respuesta = models.TextField()
    fecha = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.usuario.username} - {self.pregunta[:50]}"

    class Meta:
        ordering = ['-fecha']


class ModuloSistema(models.Model):
    """Modelo ancla para los permisos dinámicos del ERP."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
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
        ]



class SolicitudRecuperacionPassword(models.Model):
    """
    Solicitud de recuperación de contraseña controlada por la empresa
    (Fase 4). El usuario solicita, un administrador con el permiso
    'core.gestionar_recuperaciones' autoriza o rechaza, y solo
    entonces el propio usuario puede establecer su nueva contraseña
    usando el hashing nativo de Django (set_password). El
    administrador nunca ve ni define la contraseña.

    Se relaciona directamente con auth.User (no con Empleado): en los
    datos reales del proyecto ningún Empleado tiene un usuario
    vinculado todavía, así que una relación vía Empleado dejaría el
    flujo inutilizable para la mayoría de las cuentas reales. User es
    la relación real y siempre disponible para cualquier cuenta con
    acceso al sistema.
    """

    class Estado(models.TextChoices):
        PENDIENTE = 'Pendiente', 'Pendiente'
        APROBADA = 'Aprobada', 'Aprobada'
        RECHAZADA = 'Rechazada', 'Rechazada'
        COMPLETADA = 'Completada', 'Completada'

    usuario = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='solicitudes_recuperacion',
    )
    fecha_solicitud = models.DateTimeField(auto_now_add=True)
    estado = models.CharField(
        max_length=12, choices=Estado.choices, default=Estado.PENDIENTE,
    )
    fecha_resolucion = models.DateTimeField(null=True, blank=True)
    administrador_resolutor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='solicitudes_recuperacion_resueltas',
    )
    fecha_completada = models.DateTimeField(null=True, blank=True)

    # Autorización temporal para establecer la nueva contraseña. NUNCA
    # se guarda el token en texto plano: solo su hash SHA-256 (igual
    # de espíritu que Django nunca guarda contraseñas en texto plano).
    # Se genera al AUTORIZAR, no al solicitar.
    token_hash = models.CharField(max_length=64, blank=True, default='')
    token_expira = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-fecha_solicitud']
        verbose_name = 'Solicitud de recuperación de contraseña'
        verbose_name_plural = 'Solicitudes de recuperación de contraseña'

    def __str__(self):
        return f"{self.usuario.username} - {self.estado} ({self.fecha_solicitud:%Y-%m-%d})"


class Proveedor(models.Model):
    """Fase 5 (Compras). Los proveedores viven en la base de datos:
    nunca se hardcodean en el código."""

    nombre = models.CharField(max_length=150, verbose_name="Nombre / Razón social")
    rnc = models.CharField(max_length=20, blank=True, verbose_name="RNC / Identificación")
    contacto = models.CharField(max_length=120, blank=True, verbose_name="Persona de contacto")
    telefono = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    direccion = models.CharField(max_length=200, blank=True)
    activo = models.BooleanField(default=True)
    fecha_registro = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.nombre

    class Meta:
        ordering = ['nombre']


class OrdenCompra(models.Model):
    """Lo que la empresa SOLICITA al proveedor. NO afecta el inventario
    (eso ocurre solo al confirmar una Recepcion)."""

    ESTADO_CHOICES = [
        ('Borrador', 'Borrador'),
        ('Enviada', 'Enviada'),
        ('Parcialmente recibida', 'Parcialmente recibida'),
        ('Recibida', 'Recibida / completada'),
        ('Cancelada', 'Cancelada'),
    ]

    proveedor = models.ForeignKey(Proveedor, on_delete=models.PROTECT, related_name='ordenes_compra')
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    estado = models.CharField(max_length=25, choices=ESTADO_CHOICES, default='Borrador')
    creado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='ordenes_compra_creadas')
    notas = models.TextField(blank=True)

    def __str__(self):
        return f"OC #{self.pk} - {self.proveedor.nombre}"

    @property
    def total_estimado(self):
        # Las listas pueden traer este total ya calculado en PostgreSQL.
        # Si no existe la anotación, se conserva el cálculo histórico.
        if hasattr(self, 'total_estimado_calc'):
            return self.total_estimado_calc or Decimal('0')
        return sum((d.cantidad_solicitada * d.costo_unitario for d in self.detalles.all()), Decimal('0'))

    @property
    def totalmente_recibida(self):
        detalles = list(self.detalles.all())
        return bool(detalles) and all(d.cantidad_pendiente <= 0 for d in detalles)

    @property
    def parcialmente_recibida(self):
        detalles = list(self.detalles.all())
        return any(d.cantidad_recibida > 0 for d in detalles) and not self.totalmente_recibida

    class Meta:
        ordering = ['-fecha_creacion']


class DetalleOrdenCompra(models.Model):
    orden = models.ForeignKey(OrdenCompra, on_delete=models.CASCADE, related_name='detalles')
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name='detalles_orden_compra')
    cantidad_solicitada = models.PositiveIntegerField()
    costo_unitario = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.producto.nombre} x{self.cantidad_solicitada}"

    @property
    def subtotal(self):
        return self.cantidad_solicitada * self.costo_unitario

    @property
    def cantidad_recibida(self):
        # Las vistas de Compras precalculan este total en una sola consulta.
        # Mantener el fallback hace que cualquier uso fuera de esas vistas siga
        # funcionando exactamente igual.
        if hasattr(self, 'cantidad_recibida_calc'):
            return self.cantidad_recibida_calc or 0
        total = self.recepciones.aggregate(total=models.Sum('cantidad_recibida'))['total']
        return total or 0

    @property
    def cantidad_pendiente(self):
        return max(self.cantidad_solicitada - self.cantidad_recibida, 0)

    class Meta:
        ordering = ['id']


class Recepcion(models.Model):
    """Lo que FISICAMENTE llega a la empresa. Confirmar una recepción
    genera entradas reales de inventario (MovimientoStock), reutilizando
    exactamente el mismo mecanismo que ya usa Inventario: no se crea un
    segundo sistema de movimientos de stock."""

    orden = models.ForeignKey(OrdenCompra, on_delete=models.PROTECT, related_name='recepciones')
    fecha = models.DateTimeField(auto_now_add=True)
    recibido_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='recepciones_registradas')
    notas = models.TextField(blank=True)

    def __str__(self):
        return f"Recepción #{self.pk} - Orden #{self.orden_id}"

    class Meta:
        ordering = ['-fecha']


class DetalleRecepcion(models.Model):
    recepcion = models.ForeignKey(Recepcion, on_delete=models.CASCADE, related_name='detalles')
    detalle_orden = models.ForeignKey(DetalleOrdenCompra, on_delete=models.PROTECT, related_name='recepciones')
    cantidad_recibida = models.PositiveIntegerField()

    def __str__(self):
        return f"{self.detalle_orden.producto.nombre} x{self.cantidad_recibida}"

    class Meta:
        ordering = ['id']


class PedidoCliente(models.Model):
    """Solicitud previa a una Venta. No afecta stock hasta el despacho."""

    ESTADO_CHOICES = [
        ('Pendiente', 'Pendiente'),
        ('Confirmado', 'Confirmado'),
        ('Completado', 'Completado'),
        ('Rechazado', 'Rechazado'),
        ('Cancelado', 'Cancelado'),
    ]

    cliente = models.ForeignKey(Cliente, on_delete=models.PROTECT, related_name='pedidos')
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_entrega = models.DateField(null=True, blank=True, verbose_name="Fecha de entrega solicitada")
    fecha_vigencia = models.DateField(null=True, blank=True, verbose_name='Vigencia de la cotización (hasta)')
    estado = models.CharField(max_length=10, choices=ESTADO_CHOICES, default='Pendiente')
    observaciones = models.TextField(blank=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    creado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='pedidos_clientes_creados')

    def __str__(self):
        return f"Pedido #{self.pk} - {self.cliente.nombre}"

    @property
    def total_calculado(self):
        return sum((d.subtotal for d in self.detalles.all()), Decimal('0'))

    @property
    def completamente_facturado(self):
        detalles = list(self.detalles.all())
        return bool(detalles) and all(d.cantidad_pendiente <= 0 for d in detalles)

    @property
    def vencido(self):
        if not self.fecha_vigencia or self.estado != 'Pendiente':
            return False
        return self.fecha_vigencia < timezone.localdate()

    @property
    def estado_procesamiento(self):
        if self.estado in ('Rechazado', 'Cancelado'):
            return self.estado
        detalles = list(self.detalles.all())
        total_solicitado = sum(d.cantidad for d in detalles)
        total_procesado = sum(d.cantidad_facturada for d in detalles)
        if total_procesado <= 0:
            return 'Pendiente'
        if total_procesado < total_solicitado:
            return 'Entrega parcial'
        return 'Facturado'

    class Meta:
        ordering = ['-fecha_creacion']
        verbose_name = 'Pedido de cliente'
        verbose_name_plural = 'Pedidos de clientes'



class DetallePedidoCliente(models.Model):
    pedido = models.ForeignKey(PedidoCliente, on_delete=models.CASCADE, related_name='detalles')
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name='detalles_pedido_cliente')
    cantidad = models.PositiveIntegerField()
    # Se conserva el precio del momento del pedido, igual que
    # DetalleVenta.precio_unitario, para no verse afectado si el
    # precio del producto cambia despues.
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.producto.nombre} x{self.cantidad}"

    @property
    def subtotal(self):
        return self.cantidad * self.precio_unitario

    @property
    def cantidad_facturada(self):
        # Mismo patrón que DetalleOrdenCompra.cantidad_recibida: se
        # deriva de las Ventas reales relacionadas, no se guarda como
        # campo propio (evita una segunda fuente de verdad). Cuando una
        # vista ya lo anotó, se reutiliza para evitar una consulta por línea.
        if hasattr(self, 'cantidad_facturada_calc'):
            return self.cantidad_facturada_calc or 0
        total = self.ventas.aggregate(total=models.Sum('cantidad'))['total']
        return total or 0

    @property
    def cantidad_pendiente(self):
        return max(self.cantidad - self.cantidad_facturada, 0)

    class Meta:
        ordering = ['id']


class Compra(models.Model):
    CONDICION_CHOICES = [
        ('Contado', 'Contado'),
        ('Credito', 'Crédito'),
    ]
    ESTADO_PAGO_CHOICES = [
        ('Pendiente', 'Pendiente de pago'),
        ('Parcial', 'Parcialmente pagada'),
        ('Pagada', 'Pagada'),
    ]

    proveedor = models.ForeignKey(Proveedor, on_delete=models.PROTECT, related_name='compras')
    orden = models.ForeignKey(OrdenCompra, on_delete=models.SET_NULL, null=True, blank=True, related_name='compras')
    recepcion = models.ForeignKey(Recepcion, on_delete=models.SET_NULL, null=True, blank=True, related_name='compras')
    numero_factura = models.CharField(max_length=30, blank=True, verbose_name="Factura del proveedor")
    fecha = models.DateField(default=timezone.now)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    impuestos = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    condicion_pago = models.CharField(max_length=10, choices=CONDICION_CHOICES, default='Contado')
    estado_pago = models.CharField(max_length=10, choices=ESTADO_PAGO_CHOICES, default='Pendiente')
    monto_pagado = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    fecha_vencimiento = models.DateField(null=True, blank=True)
    creado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='compras_registradas')
    fecha_registro = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Compra #{self.pk} - {self.proveedor.nombre}"

    @property
    def saldo_pendiente(self):
        return max(self.total - self.monto_pagado, Decimal('0'))

    @property
    def vencida(self):
        if self.estado_pago == 'Pagada' or not self.fecha_vencimiento:
            return False
        return self.fecha_vencimiento < timezone.now().date()

    @property
    def estado_cxp(self):
        if self.estado_pago == 'Pagada':
            return 'Pagada'
        if self.vencida:
            return 'Vencida'
        if self.estado_pago == 'Parcial':
            return 'Parcial'
        return 'Pendiente'

    class Meta:
        ordering = ['-fecha_registro']
        verbose_name_plural = 'Compras'

class CuentaPorCobrar(models.Model):
    ESTADO_PENDIENTE = 'Pendiente'
    ESTADO_VENCIDA = 'Vencida'
    ESTADO_PAGADA = 'Pagada'
    ESTADO_CHOICES = [
        (ESTADO_PENDIENTE, 'Pendiente'),
        (ESTADO_VENCIDA, 'Vencida'),
        (ESTADO_PAGADA, 'Pagada'),
    ]

    venta = models.OneToOneField(Venta, on_delete=models.PROTECT, related_name='cuenta_por_cobrar')
    cliente = models.ForeignKey(Cliente, on_delete=models.PROTECT, related_name='cuentas_por_cobrar')
    importe_original = models.DecimalField(max_digits=12, decimal_places=2)
    fecha_emision = models.DateField()
    fecha_vencimiento = models.DateField()
    creado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='cuentas_por_cobrar_creadas')
    fecha_registro = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"CxC #{self.pk} - {self.cliente.nombre}"

    @property
    def total_cobrado(self):
        total = self.cobros.aggregate(total=models.Sum('monto'))['total']
        return total or Decimal('0')

    @property
    def saldo_pendiente(self):
        return max(self.importe_original - self.total_cobrado, Decimal('0'))

    @property
    def estado(self):
        if self.saldo_pendiente <= 0:
            return self.ESTADO_PAGADA
        if self.fecha_vencimiento < timezone.now().date():
            return self.ESTADO_VENCIDA
        return self.ESTADO_PENDIENTE

    class Meta:
        ordering = ['-fecha_emision']
        verbose_name = 'Cuenta por Cobrar'
        verbose_name_plural = 'Cuentas por Cobrar'


class Cobro(models.Model):
    METODO_PAGO_CHOICES = MovimientoFinanciero.MEDIO_PAGO_CHOICES

    cuenta = models.ForeignKey(CuentaPorCobrar, on_delete=models.PROTECT, related_name='cobros')
    monto = models.DecimalField(max_digits=12, decimal_places=2)
    fecha = models.DateField(default=timezone.now)
    metodo_pago = models.CharField(max_length=20, choices=METODO_PAGO_CHOICES, blank=True)
    referencia = models.CharField(max_length=60, blank=True)
    observacion = models.CharField(max_length=250, blank=True)
    registrado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='cobros_registrados')
    fecha_registro = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Cobro #{self.pk} - RD$ {self.monto} - CxC #{self.cuenta_id}"

    class Meta:
        ordering = ['-fecha_registro']
        verbose_name = 'Cobro'
        verbose_name_plural = 'Cobros'


class PagoCompra(models.Model):
    METODO_PAGO_CHOICES = MovimientoFinanciero.MEDIO_PAGO_CHOICES

    compra = models.ForeignKey(Compra, on_delete=models.PROTECT, related_name='pagos')
    monto = models.DecimalField(max_digits=12, decimal_places=2)
    fecha = models.DateField(default=timezone.now)
    metodo_pago = models.CharField(max_length=20, choices=METODO_PAGO_CHOICES, blank=True)
    referencia = models.CharField(max_length=60, blank=True)
    observacion = models.CharField(max_length=250, blank=True)
    registrado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='pagos_compra_registrados')
    fecha_registro = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Pago #{self.pk} - RD$ {self.monto} - Compra #{self.compra_id}"

    class Meta:
        ordering = ['-fecha_registro']
        verbose_name = 'Pago a proveedor'
        verbose_name_plural = 'Pagos a proveedores'


class CuentaBancaria(models.Model):
    TIPO_CHOICES = [
        ('Banco', 'Cuenta Bancaria'),
        ('Caja', 'Caja / Efectivo'),
        ('Tarjeta', 'Tarjetas por liquidar'),
    ]

    nombre = models.CharField(max_length=100)
    tipo = models.CharField(max_length=10, choices=TIPO_CHOICES, default='Banco')
    banco = models.CharField(max_length=100, blank=True)
    numero_cuenta = models.CharField(max_length=40, blank=True)
    activa = models.BooleanField(default=True)

    def __str__(self):
        return self.nombre

    class Meta:
        ordering = ['nombre']

