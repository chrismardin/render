from django.apps import apps as global_apps
from django.contrib.auth.management import create_permissions
from django.db import migrations


ADMINISTRADOR = 'Administrador'


def configurar_administrador(apps, schema_editor):
    config_core = global_apps.get_app_config('core')
    create_permissions(config_core, verbosity=0)

    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    grupo, _ = Group.objects.get_or_create(name=ADMINISTRADOR)
    grupo.permissions.add(*Permission.objects.filter(content_type__app_label='core'))


def revertir_configuracion(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    grupo = Group.objects.filter(name=ADMINISTRADOR).first()
    if grupo is not None:
        grupo.permissions.remove(*Permission.objects.filter(content_type__app_label='core'))


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0018_seed_permisos_pedidos_clientes'),
    ]

    operations = [
        migrations.RunPython(configurar_administrador, revertir_configuracion),
    ]