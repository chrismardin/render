"""
Regla de negocio del rol especial "Administrador": el sistema nunca
debe quedarse sin ningun Administrador activo.

Este archivo NO participa en el control de acceso a paginas (eso lo
hace core/permisos_django.py, via Django Group + Permission +
has_perm). Su unica responsabilidad es esta regla de integridad,
usada tanto por core/views.py como por core/admin.py y
core/proteccion_administrador.py.

(Historial: este archivo tenia antes un sistema de permisos paralelo
basado en core.models.PermisoPagina/Empleado.departamento, ya
retirado por no tener ningun uso real desde que las vistas se
migraron a Django Permission + has_perm.)
"""
from django.contrib.auth.models import User
from django.db.models import Q

ADMINISTRADOR = 'Administrador'
GRUPOS_DEL_SISTEMA = [ADMINISTRADOR]


def es_administrador(user):
    return bool(user and user.is_authenticated and (user.is_superuser or user.groups.filter(name=ADMINISTRADOR).exists()))


def cantidad_administradores_activos(excluir_id=None):
    q = User.objects.filter(is_active=True).filter(Q(is_superuser=True) | Q(groups__name=ADMINISTRADOR)).distinct()
    return q.exclude(pk=excluir_id).count() if excluir_id is not None else q.count()


def es_ultimo_administrador(usuario):
    return es_administrador(usuario) and cantidad_administradores_activos(usuario.pk) == 0
