"""
Reglas de integridad del grupo especial "Administrador", compartidas
entre las vistas personalizadas (core/views.py: roles_actualizar) y el
Django admin nativo de auth.User / auth.Group (core/admin.py).

El objetivo es que "el sistema nunca debe quedarse sin Administrador"
sea una única fuente de verdad, no dos implementaciones distintas que
puedan desincronizarse: cualquier camino (vistas propias o /admin/)
usa las mismas funciones de aquí.

No introduce ningún rol de negocio hardcodeado: solo conoce el nombre
del rol protegido (core.permisos.ADMINISTRADOR) y, para el caso de
permisos críticos, dos codenames concretos del catálogo dinámico ya
existente (core.models.ModuloSistema), no una lista de roles.
"""
from django.contrib.auth.models import Group, Permission

from .permisos import ADMINISTRADOR, cantidad_administradores_activos

# Permisos sin los cuales el grupo Administrador dejaría de poder
# administrar el sistema por sí mismo: gestionar roles/permisos y
# gestionar usuarios (asignar roles, activar/desactivar cuentas). No
# es una lista de roles de negocio: son los 2 permisos concretos del
# catálogo dinámico (core.models.ModuloSistema.Meta.permissions) que
# hacen posible seguir operando "Usuarios -> Roles y Permisos". Sin
# ellos, ni siquiera un Administrador podría revertir un error de
# configuración desde la propia interfaz, dejando el sistema
# efectivamente inutilizable aunque el grupo siga existiendo.
CODENAMES_CRITICOS_ADMINISTRADOR = {'gestionar_roles', 'ver_usuarios'}


def es_grupo_administrador(grupo):
    """True si `grupo` es el rol protegido del sistema."""
    return grupo is not None and grupo.name == ADMINISTRADOR


def permisos_criticos_faltantes(ids_permisos_nuevos):
    """
    Dado un iterable de IDs de Permission que tendría el grupo
    Administrador tras un cambio, devuelve el subconjunto de
    CODENAMES_CRITICOS_ADMINISTRADOR que NO estaría presente.

    Un resultado no vacío significa que ese cambio no debe permitirse.
    """
    codenames_presentes = set(
        Permission.objects.filter(
            id__in=list(ids_permisos_nuevos),
            content_type__app_label='core',
        ).values_list('codename', flat=True)
    )
    return CODENAMES_CRITICOS_ADMINISTRADOR - codenames_presentes


def cambio_dejaria_sistema_sin_administrador(
    usuario, *, nuevo_is_active, nuevo_is_superuser, ids_grupos_nuevos,
):
    """
    True si, tras aplicar estos cambios hipotéticos a `usuario`, el
    sistema se quedaría sin ningún Administrador activo.

    Pensada para el Django admin nativo de User, donde is_active,
    is_superuser y groups se editan juntos en el mismo formulario (a
    diferencia de core/views.py, donde cada acción es una vista
    separada y ya tiene su propia comprobación con
    es_ultimo_administrador). Reutiliza cantidad_administradores_activos
    de core/permisos.py en vez de recalcular el criterio de "quién
    cuenta como administrador".
    """
    grupo_admin = Group.objects.filter(name=ADMINISTRADOR).first()
    seguira_siendo_administrador = nuevo_is_active and (
        nuevo_is_superuser
        or (grupo_admin is not None and grupo_admin.pk in ids_grupos_nuevos)
    )
    if seguira_siendo_administrador:
        return False
    return cantidad_administradores_activos(excluir_id=usuario.pk) == 0
