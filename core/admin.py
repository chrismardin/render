from django import forms
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin, UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError

from .models import Producto, MovimientoStock, MovimientoFinanciero, Cliente, Venta, DetalleVenta, Empleado, ReporteGenerado, RegistroActividad, DatosEmpresa, PreferenciasNotificacion
from .permisos import ADMINISTRADOR
from .proteccion_administrador import (
    cambio_dejaria_sistema_sin_administrador,
    es_grupo_administrador,
    permisos_criticos_faltantes,
)


@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = ('sku', 'nombre', 'categoria', 'precio_venta', 'stock', 'estado_stock')
    list_filter = ('categoria',)
    search_fields = ('sku', 'nombre')


@admin.register(MovimientoStock)
class MovimientoStockAdmin(admin.ModelAdmin):
    list_display = ('fecha', 'producto', 'tipo', 'cantidad', 'motivo')
    list_filter = ('tipo',)


@admin.register(MovimientoFinanciero)
class MovimientoFinancieroAdmin(admin.ModelAdmin):
    list_display = ('fecha', 'tipo', 'categoria', 'cliente_proveedor', 'monto', 'medio_pago')
    list_filter = ('tipo', 'medio_pago')
    search_fields = ('categoria', 'cliente_proveedor', 'factura')


@admin.register(Cliente)
class ClienteAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'empresa', 'telefono', 'tipo')
    search_fields = ('nombre', 'empresa')


class DetalleVentaInline(admin.TabularInline):
    model = DetalleVenta
    extra = 1


@admin.register(Venta)
class VentaAdmin(admin.ModelAdmin):
    list_display = ('id', 'fecha', 'cliente', 'total', 'metodo_pago')
    inlines = [DetalleVentaInline]


admin.site.register([Empleado, ReporteGenerado, RegistroActividad, DatosEmpresa, PreferenciasNotificacion])


# ============================================================
# PROTECCIÓN DEL ÚLTIMO ADMINISTRADOR EN /admin/
#
# El admin nativo de auth.User / auth.Group permite editar y eliminar
# libremente por defecto, sin conocer la regla de negocio "el sistema
# nunca debe quedarse sin Administrador" que las vistas de
# core/views.py sí aplican. Esto reemplaza los ModelAdmin por defecto
# (registrados automáticamente por django.contrib.auth) por versiones
# que aplican exactamente la misma regla, usando
# core/proteccion_administrador.py como única fuente de verdad
# compartida con las vistas.
# ============================================================

class UsuarioAdminForm(UserChangeForm):
    """
    Aplica también desde /admin/ la regla de integridad que ya
    protege a los usuarios en core/views.py (usuarios_editar /
    usuarios_desactivar). Se valida en el formulario (clean), antes
    de guardar nada, para que el bloqueo se muestre como un error de
    formulario normal en vez de dejar el sistema sin Administrador.
    """

    class Meta(UserChangeForm.Meta):
        model = User

    def clean(self):
        cleaned_data = super().clean()
        usuario = self.instance
        if usuario.pk and cambio_dejaria_sistema_sin_administrador(
            usuario,
            nuevo_is_active=cleaned_data.get('is_active', usuario.is_active),
            nuevo_is_superuser=cleaned_data.get('is_superuser', usuario.is_superuser),
            ids_grupos_nuevos={g.pk for g in cleaned_data.get('groups') or []},
        ):
            raise ValidationError(
                'No puedes guardar este cambio: el sistema se quedaría sin '
                'ningún Administrador activo.'
            )
        return cleaned_data


admin.site.unregister(User)


@admin.register(User)
class UsuarioAdmin(DjangoUserAdmin):
    form = UsuarioAdminForm

    def has_delete_permission(self, request, obj=None):
        # Bloquea tanto el borrado individual como la acción masiva
        # "Delete selected users" (ambas consultan este método por
        # objeto antes de permitir la eliminación real).
        if obj is not None and cambio_dejaria_sistema_sin_administrador(
            obj, nuevo_is_active=False, nuevo_is_superuser=False, ids_grupos_nuevos=set(),
        ):
            return False
        return super().has_delete_permission(request, obj)


class GrupoAdminForm(forms.ModelForm):
    class Meta:
        model = Group
        fields = '__all__'

    def clean_name(self):
        nombre = self.cleaned_data['name']
        if self.instance.pk and es_grupo_administrador(self.instance) and nombre != ADMINISTRADOR:
            raise ValidationError(
                f'"{ADMINISTRADOR}" es el rol protegido del sistema y no puede renombrarse.'
            )
        return nombre

    def clean_permissions(self):
        permisos = self.cleaned_data.get('permissions')
        if self.instance.pk and es_grupo_administrador(self.instance):
            faltantes = permisos_criticos_faltantes([p.pk for p in (permisos or [])])
            if faltantes:
                raise ValidationError(
                    f'El rol "{ADMINISTRADOR}" no puede quedarse sin estos permisos '
                    f'críticos para administrar el sistema: {", ".join(sorted(faltantes))}.'
                )
        return permisos


admin.site.unregister(Group)


@admin.register(Group)
class GrupoAdmin(DjangoGroupAdmin):
    form = GrupoAdminForm

    def has_delete_permission(self, request, obj=None):
        # El grupo Administrador nunca puede eliminarse, ni siquiera
        # por un superusuario, ni individualmente ni vía la acción
        # masiva "Delete selected groups".
        if obj is not None and es_grupo_administrador(obj):
            return False
        return super().has_delete_permission(request, obj)

from .models import Nomina, DetalleNomina, TramoISR, TopeTSS, Ausencia, DiaFeriado

for _modelo in (Nomina, DetalleNomina, TramoISR, TopeTSS, Ausencia, DiaFeriado):
    try:
        admin.site.register(_modelo)
    except admin.sites.AlreadyRegistered:
        pass
