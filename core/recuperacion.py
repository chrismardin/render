"""
Fase 4 — utilidades de token para la recuperación de contraseñas
controlada por la empresa (Usuarios -> Recuperación de contraseñas).

El token que viaja en la URL nunca se guarda en la base de datos: solo
se guarda su hash SHA-256 (core.models.SolicitudRecuperacionPassword.
token_hash), igual en espíritu a como Django nunca guarda contraseñas
en texto plano. Esto sigue el mismo principio que el hashing nativo de
contraseñas de Django, aplicado aquí a un secreto de un solo uso en
vez de a una contraseña.
"""
import hashlib
from datetime import timedelta

from django.utils import timezone
from django.utils.crypto import get_random_string

DURACION_AUTORIZACION = timedelta(hours=24)


def _hash_token(token_plano):
    return hashlib.sha256(token_plano.encode('utf-8')).hexdigest()


def generar_autorizacion(solicitud):
    """
    Genera un token aleatorio de un solo uso para que el usuario
    pueda establecer su nueva contraseña, y guarda SOLO su hash y su
    fecha de expiración en la solicitud. Devuelve el token en texto
    plano (para incluirlo en el enlace que se le muestra al usuario);
    ese valor no se conserva en ningún lado después de esta llamada.
    """
    token_plano = get_random_string(48)
    solicitud.token_hash = _hash_token(token_plano)
    solicitud.token_expira = timezone.now() + DURACION_AUTORIZACION
    solicitud.save(update_fields=['token_hash', 'token_expira'])
    return token_plano


def autorizacion_valida(solicitud, token_plano):
    """
    True solo si: la solicitud está Aprobada, tiene un token
    pendiente de usar, el token coincide, y no ha expirado. Esta
    misma función es lo único que decide si el enlace de "establecer
    nueva contraseña" funciona, tanto para evitar reutilizar un token
    ya usado (se invalida con invalidar_autorizacion) como para
    evitar usar el token de otro usuario (se compara únicamente el
    hash de ESTA solicitud, nunca contra otras).
    """
    if solicitud.estado != solicitud.Estado.APROBADA:
        return False
    if not solicitud.token_hash or not solicitud.token_expira:
        return False
    if timezone.now() > solicitud.token_expira:
        return False
    return solicitud.token_hash == _hash_token(token_plano)


def invalidar_autorizacion(solicitud):
    """Limpia el token para que no pueda volver a usarse (se llama al
    completar el cambio de contraseña y también al rechazar)."""
    solicitud.token_hash = ''
    solicitud.token_expira = None
    solicitud.save(update_fields=['token_hash', 'token_expira'])
