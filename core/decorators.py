"""
Decorador para proteger vistas según el módulo al que pertenecen.
Usa exclusivamente herramientas nativas de Django: login_required
y el sistema de Group ya definido en core/permisos.py.
"""

from .permisos_django import requiere_permiso


def modulo_requerido(nombre_modulo):
    """
    Protege una vista para que solo puedan entrar los grupos
    autorizados para `nombre_modulo` (ver MODULOS en permisos.py).

    - Usuario no autenticado -> redirigido al login (login_required).
    - Usuario autenticado sin permiso -> 403 (PermissionDenied).

    Esto es lo que impide el acceso aunque el enlace del menú esté
    oculto: la protección real está aquí, no en el template.
    """
    return requiere_permiso(nombre_modulo)
