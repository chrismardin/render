from django.db import migrations


PERMISOS = {
    'ver_impuestos': 'Puede ver Impuestos',
    'ver_modo_ia': 'Puede ver Modo IA',
}

PERMISOS_POR_ROL = {
    'Administrador': ['ver_impuestos', 'ver_modo_ia'],
    'Gerente': ['ver_impuestos', 'ver_modo_ia'],
    'Finanzas': ['ver_impuestos'],
}


def otorgar_permisos(apps, schema_editor):
    """
    Crea de forma explícita los permisos nuevos y los asigna a los roles.

    No usamos django.contrib.auth.management.create_permissions() con el
    AppConfig histórico de migrations porque Django 6.1 puede entregar un
    AppConfigStub sin ``models_module`` y provocar:
    AttributeError: 'AppConfigStub' object has no attribute 'models_module'.
    """
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    Group = apps.get_model('auth', 'Group')

    # ModuloSistema es el modelo ancla donde se declaran los permisos
    # personalizados del sistema. Se asegura que su ContentType exista.
    content_type, _ = ContentType.objects.get_or_create(
        app_label='core',
        model='modulosistema',
    )

    permisos_creados = {}
    for codename, nombre in PERMISOS.items():
        permiso, _ = Permission.objects.get_or_create(
            content_type=content_type,
            codename=codename,
            defaults={'name': nombre},
        )
        # Si ya existía con otro nombre, mantenerlo coherente.
        if permiso.name != nombre:
            permiso.name = nombre
            permiso.save(update_fields=['name'])
        permisos_creados[codename] = permiso

    for nombre_rol, codenames in PERMISOS_POR_ROL.items():
        grupo = Group.objects.filter(name=nombre_rol).first()
        if grupo is None:
            continue
        grupo.permissions.add(
            *(permisos_creados[codename] for codename in codenames)
        )


def revertir_permisos(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    for nombre_rol, codenames in PERMISOS_POR_ROL.items():
        grupo = Group.objects.filter(name=nombre_rol).first()
        if grupo is None:
            continue
        permisos = Permission.objects.filter(
            content_type__app_label='core',
            content_type__model='modulosistema',
            codename__in=codenames,
        )
        grupo.permissions.remove(*permisos)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0024_impuestos_modo_ia'),
    ]

    operations = [
        migrations.RunPython(otorgar_permisos, revertir_permisos),
    ]
