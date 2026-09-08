from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand

from core.permisos import ADMINISTRADOR, GRUPOS_DEL_SISTEMA


class Command(BaseCommand):
    help = (
        "Refuerzo manual opcional del bootstrap del rol Administrador. "
        "La fuente de verdad principal es la migración "
        "0019_bootstrap_administrador, que ya se ejecuta automáticamente "
        "con 'migrate' y asigna también los permisos de core. Este comando "
        "es idempotente y no crea ningún otro rol; solo es útil si hace "
        "falta re-verificar el bootstrap manualmente (por ejemplo, tras "
        "editar el grupo a mano) o agregar superusuarios existentes al "
        "grupo Administrador para que la interfaz muestre su rol."
    )

    def handle(self, *args, **kwargs):
        creados = 0
        for nombre in GRUPOS_DEL_SISTEMA:
            _, fue_creado = Group.objects.get_or_create(name=nombre)
            if fue_creado:
                creados += 1

        # A todo superusuario existente se le agrega también el grupo
        # Administrador, solo para que la interfaz de Usuarios muestre
        # su rol correctamente. El acceso real ya lo tiene por ser
        # superusuario; esto NO cambia sus permisos ni su contraseña.
        grupo_admin = Group.objects.get(name=ADMINISTRADOR)
        agregados = 0
        for superusuario in User.objects.filter(is_superuser=True):
            if not superusuario.groups.filter(pk=grupo_admin.pk).exists():
                superusuario.groups.add(grupo_admin)
                agregados += 1

        self.stdout.write(self.style.SUCCESS(
            f"Listo. Se crearon {creados} grupos nuevos. "
            f"Se agregaron {agregados} superusuario(s) al grupo Administrador."
        ))
