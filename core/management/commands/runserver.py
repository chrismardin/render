"""Servidor de desarrollo ajustado para PostgreSQL remoto.

Django ``runserver`` crea hilos por defecto. Las conexiones de base de datos
son locales al hilo, por lo que con Neon remoto los hilos efímeros reducen el
beneficio de ``CONN_MAX_AGE``. Para este proyecto académico se usa un solo hilo
en DESARROLLO: así ``python manage.py runserver`` puede reutilizar la conexión
persistente sin exigir un script PowerShell especial.

Esto NO afecta un despliegue real (Gunicorn/Uvicorn/hosting), porque allí no se
usa el comando runserver.
"""
import os

from django.core.management.commands.runserver import Command as DjangoRunserverCommand


class Command(DjangoRunserverCommand):
    help = DjangoRunserverCommand.help + " (optimizado para BD PostgreSQL remota)"

    def handle(self, *args, **options):
        # Se puede restaurar el comportamiento multihilo temporalmente con:
        # $env:CROMF_RUNSERVER_THREADED="1"; python manage.py runserver
        usar_hilos = os.getenv('CROMF_RUNSERVER_THREADED', '').strip().lower() in {
            '1', 'true', 'yes', 'on'
        }
        if not usar_hilos:
            options['use_threading'] = False
        return super().handle(*args, **options)
