"""
Servicio central de cálculo de nómina (RRHH - Fase 3).

El ERP conserva una corrida mensual como fuente de verdad histórica.
Cada empleado puede marcarse como Mensual o Quincenal; cuando es Quincenal
se guarda además un desglose informativo de primera y segunda quincena dentro
del mismo snapshot mensual, sin duplicar corridas ni alterar el total neto.

Reglas generales de este módulo:

- Todo cálculo monetario usa Decimal. Nunca float.
- Política de redondeo: cada función que produce un monto final
  (AFP, SFS, ISR, pago de horas extra, descuento de ausencias,
  regalía) redondea explícitamente a 2 decimales con ROUND_HALF_UP
  (redondeo comercial estándar, no "banker's rounding") antes de
  devolver el valor. Los cálculos intermedios (bases, valor por hora,
  valor por día) NO se redondean a mitad de camino, para no acumular
  error de redondeo entre pasos.
- Todos los parámetros de negocio (porcentajes AFP/SFS, divisor de
  días del mes, factor de hora extra, topes TSS, escala ISR) se leen
  siempre de `DatosEmpresa` / `TopeTSS` / `TramoISR`. Nada de eso se
  hardcodea aquí.
- Formato de presentación (separador de miles, prefijo "RD$"): el
  proyecto YA tiene una convención para esto -
  `{% load humanize %}` + `|floatformat:2|intcomma` + "RD$ " literal
  en el template (ver core/templates/core/proyecciones.html y
  reportes.html). Este servicio no formatea nada para mostrar; solo
  calcula y devuelve Decimal. El Paso 3 (vistas/templates) reutiliza
  esa misma convención para nómina, sin introducir un helper nuevo
  que la duplicaría.
- Este módulo NO toca vistas, templates, PDF, MovimientoFinanciero
  ni el flujo de generación/pago de Nomina. Es una capa de cálculo
  aislada y testeable por separado, tal como pide la Fase 3.
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from .models import DatosEmpresa, TopeTSS, TramoISR

CENTAVOS = Decimal('0.01')
CIEN = Decimal('100')


def redondear(valor):
    """Redondeo monetario estándar (comercial) a 2 decimales."""
    return valor.quantize(CENTAVOS, rounding=ROUND_HALF_UP)


def obtener_tope_tss(anio):
    try:
        return TopeTSS.objects.get(anio=anio)
    except TopeTSS.DoesNotExist:
        # Requisito explícito de Fase 3: el servicio debe fallar
        # claramente si no existe el TopeTSS del año, nunca continuar
        # silenciosamente "sin tope".
        raise ValueError(
            f"No hay TopeTSS sembrado para el año {anio}. "
            "Debe crearse (vía migración/seed o admin) antes de calcular nómina de ese año."
        )


def obtener_tramos_isr(anio):
    tramos = list(TramoISR.objects.filter(anio=anio).order_by('orden'))
    if not tramos:
        raise ValueError(
            f"No hay escala de ISR (TramoISR) sembrada para el año {anio}."
        )
    return tramos


def calcular_base_cotizable(salario, tope):
    """Aplica el tope de cotización TSS correspondiente (AFP o SFS)."""
    if tope is not None and salario > tope:
        return tope
    return salario


def calcular_afp(salario, anio, datos_empresa=None, tope_tss=None):
    """AFP del trabajador = base cotizable (con tope) x afp_porcentaje.

    ``datos_empresa`` y ``tope_tss`` son opcionales para conservar la API
    existente. Las vistas de RRHH los reutilizan cuando calculan muchos
    empleados, evitando dos consultas repetidas por cada empleado.
    """
    datos_empresa = datos_empresa or DatosEmpresa.obtener()
    tope_tss = tope_tss or obtener_tope_tss(anio)
    base = calcular_base_cotizable(salario, tope_tss.tope_afp)
    return redondear(base * datos_empresa.afp_porcentaje / CIEN)


def calcular_sfs(salario, anio, datos_empresa=None, tope_tss=None):
    """SFS del trabajador = base cotizable (con tope) x sfs_porcentaje."""
    datos_empresa = datos_empresa or DatosEmpresa.obtener()
    tope_tss = tope_tss or obtener_tope_tss(anio)
    base = calcular_base_cotizable(salario, tope_tss.tope_sfs)
    return redondear(base * datos_empresa.sfs_porcentaje / CIEN)


def aplicar_escala_isr(base_imponible_anual, anio, tramos=None):
    """
    Aplica la escala progresiva de ISR (DGII) sobre una base imponible
    ANUAL ya determinada, consultando TramoISR — sin ningún límite,
    tasa o monto acumulado escrito en este código.
    """
    if base_imponible_anual <= 0:
        return Decimal('0.00')

    tramos = tramos if tramos is not None else obtener_tramos_isr(anio)
    tramo_aplicable = None
    for tramo in tramos:
        supera_inferior = base_imponible_anual > tramo.limite_inferior
        dentro_de_tope = tramo.limite_superior is None or base_imponible_anual <= tramo.limite_superior
        if supera_inferior and dentro_de_tope:
            tramo_aplicable = tramo
            break

    if tramo_aplicable is None:
        # base_imponible_anual <= limite_inferior del primer tramo:
        # cae en el tramo exento.
        return Decimal('0.00')

    excedente = base_imponible_anual - tramo_aplicable.limite_inferior
    return redondear(tramo_aplicable.monto_acumulado + excedente * tramo_aplicable.tasa / CIEN)


def calcular_valor_hora(salario, datos_empresa=None):
    datos_empresa = datos_empresa or DatosEmpresa.obtener()
    return salario / datos_empresa.dias_mes_calculo / Decimal('8')


def calcular_pago_horas_extra(salario, horas_extra, datos_empresa=None):
    datos_empresa = datos_empresa or DatosEmpresa.obtener()
    if not horas_extra:
        return Decimal('0.00')
    valor_hora = calcular_valor_hora(salario, datos_empresa=datos_empresa)
    return redondear(valor_hora * datos_empresa.factor_hora_extra * Decimal(horas_extra))


def calcular_descuento_ausencias(salario, dias_ausencia, datos_empresa=None):
    datos_empresa = datos_empresa or DatosEmpresa.obtener()
    if not dias_ausencia:
        return Decimal('0.00')
    valor_dia = salario / datos_empresa.dias_mes_calculo
    return redondear(valor_dia * Decimal(dias_ausencia))


def calcular_costos_patronales(nomina_base, datos_empresa=None):
    """Calcula los aportes patronales añadidos desde rosaFinal.

    INFOTEP se calcula al 1% de la nómina base y Riesgo Laboral SRL
    usa el porcentaje configurable en DatosEmpresa. Estos montos son
    costos de la empresa y NO se descuentan del salario del empleado.
    """
    datos_empresa = datos_empresa or DatosEmpresa.obtener()
    base = Decimal(nomina_base or 0)
    infotep = redondear(base * Decimal('1.00') / CIEN)
    riesgo_laboral = redondear(base * datos_empresa.porcentaje_riesgo_laboral / CIEN)
    return {
        'infotep': infotep,
        'riesgo_laboral': riesgo_laboral,
        'total_costo_patronal': redondear(infotep + riesgo_laboral),
    }


def calcular_antiguedad_anios(empleado, fecha_referencia=None):
    fecha_referencia = fecha_referencia or date.today()
    dias = (fecha_referencia - empleado.fecha_ingreso).days
    if dias < 0:
        return Decimal('0')
    return Decimal(dias) / Decimal('365.25')


def calcular_dias_vacaciones_correspondientes(empleado, fecha_referencia=None):
    """Regla acordada: < 5 años -> 14 días; >= 5 años -> 18 días."""
    antiguedad = calcular_antiguedad_anios(empleado, fecha_referencia)
    return 18 if antiguedad >= 5 else 14


def calcular_vacaciones(empleado, fecha_referencia=None):
    correspondientes = calcular_dias_vacaciones_correspondientes(empleado, fecha_referencia)
    tomados = empleado.dias_vacaciones_tomados or 0
    restantes = max(correspondientes - tomados, 0)
    return {
        'dias_correspondientes': correspondientes,
        'dias_tomados': tomados,
        'dias_restantes': restantes,
    }


def calcular_meses_trabajados_en_anio(empleado, anio):
    """
    Meses trabajados dentro del año `anio`, contando desde la fecha
    de ingreso del empleado (o desde enero si ingresó en un año
    anterior), hasta diciembre. Usa Empleado.fecha_ingreso existente;
    no se introduce ningún campo de fecha nuevo.
    """
    inicio_anio = date(anio, 1, 1)
    fin_anio = date(anio, 12, 31)
    inicio_efectivo = max(empleado.fecha_ingreso, inicio_anio)
    if inicio_efectivo > fin_anio:
        return 0
    meses = (fin_anio.year - inicio_efectivo.year) * 12 + (fin_anio.month - inicio_efectivo.month) + 1
    return min(meses, 12)


def calcular_regalia(empleado, anio):
    """regalía = salario x meses_trabajados_en_el_año / 12 (concepto independiente, no se suma a nómina ordinaria)."""
    meses = calcular_meses_trabajados_en_anio(empleado, anio)
    if meses <= 0:
        return Decimal('0.00')
    return redondear(empleado.salario * Decimal(meses) / Decimal('12'))


def calcular_isr_mensual(
    salario_mensual, ingreso_gravable_adicional_mes, anio,
    datos_empresa=None, tope_tss=None, tramos=None,
):
    """
    ISR mensual (única periodicidad soportada por el ERP - Fase 3,
    decisión definitiva). Empleado.salario ya es un salario mensual,
    así que el período de la Nomina y el mes fiscal coinciden siempre.

    ingreso_gravable_adicional_mes: otros conceptos gravables del mes
    (en esta fase, el pago de horas extra) — forman parte del ingreso
    sujeto a retención.

    AFP y SFS de referencia se calculan sobre el salario mensual
    completo (calcular_afp/calcular_sfs), porque son la base real de
    la resta antes de aplicar la escala de ISR.

    Devuelve (isr_mensual, afp_mensual, sfs_mensual).
    """
    datos_empresa = datos_empresa or DatosEmpresa.obtener()
    tope_tss = tope_tss or obtener_tope_tss(anio)
    tramos = tramos if tramos is not None else obtener_tramos_isr(anio)
    afp_mensual = calcular_afp(salario_mensual, anio, datos_empresa=datos_empresa, tope_tss=tope_tss)
    sfs_mensual = calcular_sfs(salario_mensual, anio, datos_empresa=datos_empresa, tope_tss=tope_tss)
    ingreso_gravable_mensual = salario_mensual + (ingreso_gravable_adicional_mes or Decimal('0'))
    base_imponible_mensual = ingreso_gravable_mensual - afp_mensual - sfs_mensual
    if base_imponible_mensual <= 0:
        return Decimal('0.00'), afp_mensual, sfs_mensual

    # RD$34,685 (exención mensual 2026) NUNCA se hardcodea: surge de
    # dividir entre 12 el límite inferior del primer tramo de
    # TramoISR, que a su vez surge de aplicar la escala anual a la
    # base ANUALIZADA (mensual x 12). No se compara la base mensual
    # directamente contra ningún número escrito en este archivo.
    base_imponible_anual = base_imponible_mensual * Decimal('12')
    isr_anual = aplicar_escala_isr(base_imponible_anual, anio, tramos=tramos)
    isr_mensual = redondear(isr_anual / Decimal('12'))
    return isr_mensual, afp_mensual, sfs_mensual

# ---------------------------------------------------------------------------
# Generación de nómina mensual.
#
# Orquesta las funciones de cálculo de arriba sobre los empleados
# activos del período. NO crea MovimientoFinanciero (eso pertenece
# exclusivamente al flujo de "registrar pago", todavía no
# implementado) y deja la Nomina en su estado por defecto
# ('Pendiente de pago').
# ---------------------------------------------------------------------------

def generar_nomina_mensual(anio, mes, generado_por=None):
    """
    Genera la nómina mensual de (anio, mes): un DetalleNomina por cada
    empleado activo, y los totales de la Nomina resultante.

    Empleados considerados: Empleado.objects.filter(estado='Activo')
    - mismo criterio ya usado en el resto del proyecto (dashboard,
    costo mensual de nómina) para "empleado operativo". No se inventa
    un campo nuevo.

    Ausencias: se cuentan las Ausencia con fecha dentro del período y
    justificada=False. Decisión tomada en este paso, la señalo
    explícitamente porque el modelo no obliga una única lectura
    posible: una ausencia JUSTIFICADA (con motivo válido/aprobado) no
    genera descuento salarial; solo las INJUSTIFICADAS lo generan.
    Es la lectura más común en la práctica laboral dominicana y la
    más conservadora para el empleado (nunca se le descuenta una
    ausencia que quedó registrada como justificada). Si la política
    real de la empresa fuera otra (p. ej. que ciertas ausencias
    justificadas también se descuenten), debe confirmarse y ajustarse
    aquí puntualmente.

    Feriados: DiaFeriado NO participa en este cálculo (ver Fase 3 -
    aclaración de feriados: un feriado no trabajado no se descuenta
    porque el salario mensual ya lo cubre por defecto, y el recargo
    por feriado trabajado no se automatiza todavía por falta de un
    registro real de asistencia).

    No crea MovimientoFinanciero. Deja la Nomina en su estado por
    defecto ('Pendiente de pago').

    Lanza ValueError si ya existe una nómina para ese mes.
    """
    import calendar
    from django.db import transaction, IntegrityError
    from .models import Nomina, DetalleNomina, Empleado, Ausencia

    fecha_inicio = date(anio, mes, 1)
    ultimo_dia_mes = calendar.monthrange(anio, mes)[1]
    fecha_fin = date(anio, mes, ultimo_dia_mes)

    mensaje_duplicado = f"Ya existe una nómina generada para {mes:02d}/{anio}."

    # Esta comprobación previa cubre el caso normal (un solo usuario,
    # un solo request) con un error controlado y legible. NO es la
    # protección definitiva contra duplicados: dos solicitudes
    # concurrentes podrían pasar esta comprobación ambas en False
    # antes de que cualquiera de las dos llegue a guardar (TOCTOU).
    # La protección real es el UniqueConstraint('fecha_inicio') de
    # Nomina.Meta (ver models.py), que la base de datos garantiza sin
    # depender de que esta vista se acuerde de revisarlo. El bloque
    # try/except de abajo solo traduce esa violación de integridad,
    # si ocurre, al mismo ValueError controlado (en vez de dejar
    # propagar un IntegrityError/500 sin manejar).
    if Nomina.objects.filter(fecha_inicio=fecha_inicio).exists():
        raise ValueError(mensaje_duplicado)

    try:
        with transaction.atomic():
            nomina = Nomina(fecha_inicio=fecha_inicio, fecha_fin=fecha_fin, generada_por=generado_por)
            nomina.full_clean()  # revalida día 1 / último día del mes (Nomina.clean())
            nomina.save()

            total_bruto = Decimal('0.00')
            total_deducciones = Decimal('0.00')
            total_neto = Decimal('0.00')

            # Carga en bloque los parámetros y ausencias del período. Antes se
            # consultaban DatosEmpresa/TopeTSS/TramoISR y se hacía un COUNT de
            # ausencias por CADA empleado; con PostgreSQL remoto eso multiplicaba
            # la latencia. La lógica contable no cambia: solo se reutilizan los
            # mismos datos durante esta corrida.
            datos_empresa = DatosEmpresa.obtener()
            tope_tss = obtener_tope_tss(anio)
            tramos = obtener_tramos_isr(anio)
            empleados_activos = list(Empleado.objects.filter(estado='Activo').order_by('nombre'))

            from django.db.models import Count
            ausencias_por_empleado = {
                fila['empleado_id']: fila['total']
                for fila in Ausencia.objects.filter(
                    empleado__in=empleados_activos,
                    fecha__gte=fecha_inicio,
                    fecha__lte=fecha_fin,
                    justificada=False,
                ).values('empleado_id').annotate(total=Count('id'))
            }

            detalles_a_crear = []
            for empleado in empleados_activos:
                salario = empleado.salario
                horas_extra = empleado.horas_extra_mes or Decimal('0')
                pago_horas_extra = calcular_pago_horas_extra(
                    salario, horas_extra, datos_empresa=datos_empresa
                )

                dias_ausencia = ausencias_por_empleado.get(empleado.id, 0)
                descuento_ausencias = calcular_descuento_ausencias(
                    salario, dias_ausencia, datos_empresa=datos_empresa
                )

                # calcular_isr_mensual sigue siendo la fuente de verdad de AFP,
                # SFS e ISR. Se le pasan los parámetros ya cargados una sola vez.
                isr, afp, sfs = calcular_isr_mensual(
                    salario, pago_horas_extra, anio,
                    datos_empresa=datos_empresa, tope_tss=tope_tss, tramos=tramos,
                )

                salario_bruto = salario + pago_horas_extra
                total_deducciones_empleado = afp + sfs + isr + descuento_ausencias
                salario_neto = salario_bruto - total_deducciones_empleado

                tipo_nomina = getattr(empleado, 'tipo_nomina', 'Mensual') or 'Mensual'
                primera_quincena = Decimal('0.00')
                segunda_quincena = Decimal('0.00')
                if tipo_nomina == 'Quincenal':
                    primera_quincena = redondear(salario / Decimal('2'))
                    segunda_quincena = redondear(salario_neto - primera_quincena)

                detalles_a_crear.append(DetalleNomina(
                    nomina=nomina,
                    empleado=empleado,
                    empleado_nombre=empleado.nombre,
                    salario_base=salario,
                    tipo_nomina=tipo_nomina,
                    primera_quincena=primera_quincena,
                    segunda_quincena=segunda_quincena,
                    dias_ausencia=dias_ausencia,
                    horas_extra=horas_extra,
                    pago_horas_extra=pago_horas_extra,
                    afp=afp,
                    sfs=sfs,
                    isr=isr,
                    descuento_ausencias=descuento_ausencias,
                    salario_bruto=salario_bruto,
                    total_deducciones=total_deducciones_empleado,
                    salario_neto=salario_neto,
                ))

                total_bruto += salario_bruto
                total_deducciones += total_deducciones_empleado
                total_neto += salario_neto

            if detalles_a_crear:
                DetalleNomina.objects.bulk_create(detalles_a_crear)

            nomina.total_bruto = total_bruto
            nomina.total_deducciones = total_deducciones
            nomina.total_neto = total_neto
            nomina.save(update_fields=['total_bruto', 'total_deducciones', 'total_neto'])
    except IntegrityError:
        # Otra solicitud concurrente ganó la carrera y ya confirmó su
        # transacción entre la comprobación de arriba y este punto.
        # transaction.atomic() ya revirtió por completo lo que esta
        # llamada alcanzó a escribir (cabecera Nomina y cualquier
        # DetalleNomina parcial) antes de relanzar la excepción, así
        # que no queda ninguna fila a medias. Se traduce a un
        # ValueError controlado, igual que la comprobación previa,
        # para que la vista lo maneje exactamente igual.
        raise ValueError(mensaje_duplicado) from None

    return nomina


