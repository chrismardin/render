"""
Lógica de apoyo para la interfaz "Roles y Permisos" (dentro de Usuarios).

Este archivo es nuevo y exclusivo de la Fase 2. No modifica ni reemplaza
core/permisos.py ni core/decorators.py (el sistema antiguo basado en
MODULOS/ESCRITURA sigue intacto y en uso para el resto de los módulos).

Su única responsabilidad es traducir los permisos "planos" de Django
(auth_permission, creados en la Fase 1 vía core.models.ModuloSistema) a
una estructura agrupada por módulo, fácil de mostrar y de procesar desde
un formulario HTML con checkboxes.
"""

from django.contrib.auth.models import Permission

# Catálogo de permisos dinámicos agrupados por módulo, en el orden en que
# deben mostrarse en la interfaz. Debe reflejar exactamente los codenames
# declarados en core.models.ModuloSistema.Meta.permissions (Fase 1).
PERMISOS_POR_MODULO = [
    ('Dashboard', [('ver_dashboard', 'Ver')]),
    ('Ventas', [('ver_ventas', 'Ver')]),
    ('Productos', [('ver_productos', 'Ver'), ('crear_productos', 'Crear / Editar')]),
    ('Finanzas', [
        ('ver_finanzas', 'Ver'),
        ('ver_cuentas_por_cobrar', 'Ver Cuentas por Cobrar'),
        ('registrar_cobros', 'Registrar cobros CxC'),
    ]),
    ('Inventario', [('ver_inventario', 'Ver')]),
    ('Proyecciones', [('ver_proyecciones', 'Ver')]),
    ('Clientes', [('ver_clientes', 'Ver')]),
    ('Pedidos de Clientes', [
        ('ver_pedidos_clientes', 'Ver'),
        ('gestionar_pedidos_clientes', 'Crear y gestionar'),
    ]),
    ('Empleados', [
        ('ver_empleados', 'Ver'),
        ('gestionar_empleados', 'Crear y editar'),
    ]),
    ('Nómina', [
        ('ver_nomina', 'Ver'),
        ('gestionar_nomina', 'Generar nómina'),
        ('registrar_pago_nomina', 'Registrar pago de nómina'),
    ]),
    ('Ausencias', [
        ('ver_ausencias', 'Ver'),
        ('gestionar_ausencias', 'Registrar y editar'),
    ]),
    ('Días Feriados', [
        ('ver_feriados', 'Ver'),
        ('gestionar_feriados', 'Crear y editar'),
    ]),
    ('Impuestos', [('ver_impuestos', 'Ver')]),
    ('Reportes', [('ver_reportes', 'Ver')]),
    ('Modo IA', [('ver_modo_ia', 'Ver')]),
    ('Configuración', [('ver_configuracion', 'Ver')]),
    ('Usuarios', [('ver_usuarios', 'Ver y gestionar')]),
    ('Roles y Permisos', [('gestionar_roles', 'Gestionar roles')]),
    ('Recuperación de contraseñas', [('gestionar_recuperaciones', 'Gestionar recuperaciones')]),
    ('Compras', [
        ('ver_compras', 'Ver'),
        ('gestionar_proveedores', 'Gestionar proveedores'),
        ('gestionar_ordenes_compra', 'Gestionar órdenes de compra'),
        ('gestionar_recepciones', 'Gestionar recepciones'),
        ('gestionar_compras', 'Gestionar compras'),
        ('registrar_pagos_compra', 'Registrar pagos (Cuentas por Pagar)'),
    ]),
]

# Todos los codenames válidos, usado para validar los IDs que llegan por
# POST antes de asignarlos a un Group (nunca se confía en el request).
_TODOS_LOS_CODENAMES = [
    codename
    for _modulo, items in PERMISOS_POR_MODULO
    for codename, _etiqueta in items
]


def construir_matriz_permisos(ids_actuales=None):
    """
    Devuelve PERMISOS_POR_MODULO "enriquecido" con el objeto Permission
    real de cada codename y si debe aparecer marcado (checked) según
    `ids_actuales` (una colección de IDs de Permission ya asignados,
    por ejemplo los de un Group que se está editando).

    Se usa tanto para editar un rol existente (Roles y Permisos) como
    para el formulario de creación de un rol nuevo (donde ids_actuales
    es None y todo aparece sin marcar).
    """
    ids_actuales = set(ids_actuales or [])

    permisos_db = {
        p.codename: p
        for p in Permission.objects.filter(
            content_type__app_label='core',
            codename__in=_TODOS_LOS_CODENAMES,
        )
    }

    matriz = []
    for modulo, items in PERMISOS_POR_MODULO:
        fila = []
        for codename, etiqueta in items:
            permiso = permisos_db.get(codename)
            if permiso is None:
                # No debería pasar si la Fase 1 se aplicó correctamente,
                # pero se ignora en vez de romper la vista.
                continue
            fila.append({
                'permiso': permiso,
                'etiqueta': etiqueta,
                'marcado': permiso.id in ids_actuales,
            })
        if fila:
            matriz.append({'modulo': modulo, 'permisos': fila})

    return matriz


def permisos_validos_desde_ids(ids_seleccionados):
    """
    Filtra una lista de IDs (tal como llegan de request.POST.getlist)
    contra los permisos reales del catálogo de core, para no confiar
    ciegamente en lo que envía el formulario.
    """
    return Permission.objects.filter(
        content_type__app_label='core',
        codename__in=_TODOS_LOS_CODENAMES,
        id__in=ids_seleccionados,
    )
