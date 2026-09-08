from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Entrena el modelo de Machine Learning (Random Forest) con el historial real de ventas.'

    def handle(self, *args, **options):
        try:
            from core.ml.prediccion import entrenar_modelo
        except ImportError as exc:
            raise CommandError(
                'Faltan dependencias de IA. Ejecuta: python -m pip install -r requirements.txt. '
                f'Detalle: {exc}'
            ) from exc

        self.stdout.write('Entrenando modelo de IA con datos reales de ventas...')
        resultado = entrenar_modelo()

        if not resultado['exito']:
            self.stdout.write(self.style.WARNING(resultado['mensaje']))
            return

        self.stdout.write(self.style.SUCCESS(
            f"Modelo entrenado correctamente con {resultado['registros_entrenamiento']} registros "
            f"({resultado['productos_distintos']} productos distintos)."
        ))
        self.stdout.write(f"Importancia de variables: {resultado['importancia_variables']}")
