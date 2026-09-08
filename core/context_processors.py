from .permisos import GRUPOS_DEL_SISTEMA, ADMINISTRADOR

# Lista de módulos con permiso "ver_<modulo>" dinámico (creados en la
# Fase 1, en core.models.ModuloSistema). Se usa únicamente para saber
# qué variables 'puede_ver_<modulo>' calcular a continuación; no
# determina el acceso real (eso ya lo hacen las vistas con
# core.permisos_django, desde la Fase 3).
MODULOS_CON_PERMISO_DINAMICO = [
    'dashboard', 'ventas', 'productos', 'finanzas', 'inventario',
    'proyecciones', 'clientes', 'empleados', 'impuestos', 'reportes',
    'modo_ia', 'configuracion', 'usuarios', 'compras', 'pedidos_clientes',
]


def modulos_visibles(request):
    """
    Agrega al contexto de TODOS los templates una variable
    'puede_ver_<modulo>' por cada módulo del sistema (por ejemplo
    puede_ver_finanzas, puede_ver_ventas...).

    Desde la Fase 3, esto se calcula con el sistema nativo de Django
    (request.user.has_perm('core.ver_<modulo>')) en vez del sistema
    antiguo basado en MODULOS/Group por nombre. Esto es lo que hace
    que un rol personalizado creado en Usuarios -> Roles y Permisos
    (Fase 2) también controle correctamente qué aparece en el sidebar,
    sin tocar código.

    Esto es SOLO para mostrar u ocultar enlaces del sidebar. El
    control de acceso real está en las vistas, protegidas con los
    decoradores de core/permisos_django.py. Ocultar un enlace aquí no
    reemplaza esa protección.
    """
    usuario = getattr(request, 'user', None)

    if not usuario or not usuario.is_authenticated:
        return {}

    return {
        f'puede_ver_{modulo}': usuario.has_perm(f'core.ver_{modulo}')
        for modulo in MODULOS_CON_PERMISO_DINAMICO
    }


def rol_actual(request):
    """
    Agrega al contexto la variable 'rol_actual': el nombre del
    grupo (rol) del usuario autenticado, para mostrarlo en el
    perfil de la topbar.

    Es SOLO de presentación (no participa en ninguna decisión de
    acceso). Desde la Fase 3 se ajustó para mostrar también roles
    personalizados creados en Roles y Permisos (antes solo reconocía
    los 5 roles originales y mostraba "Sin rol asignado" para
    cualquier rol nuevo, lo cual ya no es correcto).
    """
    usuario = getattr(request, 'user', None)

    if not usuario or not usuario.is_authenticated:
        return {}

    # Un superusuario, aunque no tenga el grupo asignado
    # explícitamente, se presenta como Administrador (coherente
    # con cómo ya lo trata es_administrador() en permisos.py).
    if usuario.is_superuser:
        return {'rol_actual': ADMINISTRADOR}

    # Antes se hacía un EXISTS para Administrador y luego otra consulta para
    # obtener el primer grupo. Una sola consulta trae los nombres y mantiene
    # exactamente la misma regla visible.
    nombres_grupos = list(usuario.groups.order_by('pk').values_list('name', flat=True))
    if ADMINISTRADOR in nombres_grupos:
        return {'rol_actual': ADMINISTRADOR}
    return {'rol_actual': nombres_grupos[0] if nombres_grupos else 'Sin rol asignado'}
