"""
Fase 3 — puente entre el control de acceso a módulos y el sistema
nativo de Django (Group + Permission + has_perm), usando los permisos
creados en la Fase 1 (core.models.ModuloSistema) y administrables
dinámicamente desde Usuarios -> Roles y Permisos (Fase 2).

IMPORTANTE: este archivo NO modifica ni reemplaza core/decorators.py
ni core/permisos.py. core/permisos.py sigue existiendo, pero reducido
a su única responsabilidad real: es_administrador() /
es_ultimo_administrador() / GRUPOS_DEL_SISTEMA, la regla de negocio de
"el sistema nunca debe quedarse sin Administrador" (distinta del
acceso a páginas, que resuelve este archivo).

Este archivo es el ÚNICO punto que decide "qué módulo puede ver o usar
cada usuario". Las vistas de core/views.py importan de aquí para ese
propósito específico.

NOTA HISTÓRICA: el proyecto tuvo antes un sistema paralelo de acceso
basado en core.models.PermisoPagina / Empleado.departamento
(core.permisos._permiso_empleado y afines). Se confirmó que la tabla
PermisoPagina estaba vacía y que ningún Empleado tenía un usuario
vinculado en los datos reales, es decir, ese camino ya no tenía ningún
efecto práctico incluso antes de migrar a has_perm(). Tras confirmarlo
mediante auditoría, ese modelo y esas funciones se eliminaron por
completo del proyecto.
"""
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

# Permiso de escritura por módulo, usando EXCLUSIVAMENTE los permisos
# ya creados en la Fase 1 (no se inventa ninguno nuevo en esta fase).
#
# Solo 'productos' tiene un permiso de escritura realmente distinto
# del de lectura (crear_productos). Para el resto de los módulos el
# sistema antiguo (core.permisos.ESCRITURA) tampoco distinguía lectura
# de escritura: usuario_puede_escribir() caía a MODULOS.get(modulo,...)
# cuando ESCRITURA no tenía una entrada para ese módulo, es decir,
# cualquiera que podía VER el módulo también podía escribir en él. Por
# eso aquí se usa el permiso de vista (ver_<modulo>) como equivalente
# para esos módulos, preservando el comportamiento actual en vez de
# inventar un permiso de escritura nuevo.
PERMISO_ESCRITURA_POR_MODULO = {
    'productos': 'crear_productos',
    'empleados': 'gestionar_empleados',
    # ventas, finanzas, inventario, clientes, empleados, reportes y
    # configuracion NO tienen un permiso de escritura propio todavía:
    # se resuelven con ver_<modulo> (ver _codename_escribir más abajo),
    # documentado aquí en vez de crear codenames nuevos.
}


def _codename_ver(modulo):
    return f'ver_{modulo}'


def _codename_escribir(modulo):
    return PERMISO_ESCRITURA_POR_MODULO.get(modulo, _codename_ver(modulo))


def requiere_permiso(modulo):
    """
    Reemplaza a @requiere_ver(modulo) de core/permisos.py.

    Comprueba request.user.has_perm('core.ver_<modulo>') (el superuser
    pasa automáticamente por las reglas nativas de Django).

    Mantiene el MISMO comportamiento visible que tenía requiere_ver:
    usuario sin permiso -> mensaje de error + redirect a 'dashboard'
    (no un 403 duro), para no cambiar la experiencia actual.
    """
    permiso = f'core.{_codename_ver(modulo)}'

    def decorador(vista):
        @wraps(vista)
        def envoltura(request, *args, **kwargs):
            if not request.user.has_perm(permiso):
                messages.error(request, 'No tienes permiso para ver esta pagina.')
                return redirect('dashboard')
            return vista(request, *args, **kwargs)
        return login_required(envoltura)
    return decorador


def permiso_modulo_requerido(modulo):
    """
    Reemplaza a @modulo_requerido(modulo) de core/decorators.py,
    usado únicamente por las vistas de Usuarios (ver_usuarios).

    Mantiene el MISMO comportamiento visible que tenía
    modulo_requerido: usuario sin permiso -> PermissionDenied (403),
    no un redirect silencioso como en requiere_permiso().
    """
    permiso = f'core.{_codename_ver(modulo)}'

    def decorador(vista):
        @wraps(vista)
        def comprobacion(request, *args, **kwargs):
            if not request.user.has_perm(permiso):
                raise PermissionDenied(
                    'No tienes permiso para acceder a este módulo.'
                )
            return vista(request, *args, **kwargs)
        return login_required(comprobacion)
    return decorador


def puede_escribir_en(request, modulo):
    """
    Reemplaza a puede_crear_en(request, modulo) de core/permisos.py,
    para las comprobaciones de escritura que se hacen DENTRO del
    cuerpo de una vista (crear/editar/eliminar), no solo al entrar a
    la página.
    """
    return request.user.has_perm(f'core.{_codename_escribir(modulo)}')
