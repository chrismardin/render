from django.db import migrations


class Migration(migrations.Migration):
    """Une las dos ramas históricas que ya coexistían en Neon.

    Rama Chris termina en 0025_seed_permisos_impuestos_ia.
    Rama Mónica termina en 0027_seed_permisos_cuentas_por_pagar.
    No modifica tablas: solo reconcilia el grafo de migraciones.
    """

    dependencies = [
        ('core', '0025_seed_permisos_impuestos_ia'),
        ('core', '0027_seed_permisos_cuentas_por_pagar'),
    ]

    operations = []
