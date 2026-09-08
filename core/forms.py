from django import forms
from django.contrib.auth.models import Group, User
from django.contrib.auth.forms import UserCreationForm

from .models import (
    MovimientoStock, MovimientoFinanciero, Producto, Proveedor,
    OrdenCompra, DetalleOrdenCompra, Recepcion, Compra,
    PedidoCliente, DetallePedidoCliente, Cobro, PagoCompra, CuentaBancaria,
)
from .permisos import GRUPOS_DEL_SISTEMA


def _asegurar_roles_base():
    # Refuerzo idempotente y de solo lectura de lo que ya garantiza la
    # migración 0019_bootstrap_administrador (fuente de verdad
    # principal): que el rol Administrador exista. No crea ningún otro
    # rol; los demás roles son siempre creados por la empresa desde
    # Usuarios -> Roles y Permisos.
    Group.objects.get_or_create(name=GRUPOS_DEL_SISTEMA[0])


class MovimientoStockForm(forms.ModelForm):
    class Meta:
        model = MovimientoStock
        fields = ['producto', 'tipo', 'cantidad', 'motivo']
        widgets = {
            'producto': forms.Select(attrs={'class': 'select-producto'}),
            'tipo': forms.Select(attrs={'class': 'select-tipo'}),
            'cantidad': forms.NumberInput(attrs={'min': 1}),
            'motivo': forms.TextInput(attrs={'placeholder': 'Ej. Compra a proveedor, Venta, Ajuste...'}),
        }


class MovimientoFinancieroForm(forms.ModelForm):
    # Categorías automáticas: no deben poder duplicarse manualmente.
    CATEGORIAS_RESERVADAS_AL_SISTEMA = {
        'venta', 'ventas', 'compras', 'cobro de venta', 'cobro cxc',
        'nomina', 'nómina', 'pago compra', 'pago de compra',
    }

    class Meta:
        model = MovimientoFinanciero
        fields = ['tipo', 'fecha', 'categoria', 'cliente_proveedor', 'monto', 'cuenta', 'medio_pago', 'factura']
        widgets = {
            'fecha': forms.DateInput(attrs={'type': 'date'}),
            'categoria': forms.TextInput(attrs={'placeholder': 'Ej. Alquiler, Electricidad, Internet...'}),
            'cliente_proveedor': forms.TextInput(attrs={'placeholder': 'Nombre del cliente o proveedor'}),
            'factura': forms.TextInput(attrs={'placeholder': 'Opcional'}),
        }

    def clean_categoria(self):
        categoria = self.cleaned_data['categoria']
        if categoria.strip().lower() in self.CATEGORIAS_RESERVADAS_AL_SISTEMA:
            raise forms.ValidationError(
                f'"{categoria}" es una categoría que el sistema genera automáticamente '
                f'y no puede crearse manualmente aquí.'
            )
        return categoria



class ProductoForm(forms.ModelForm):
    class Meta:
        model = Producto
        fields = [
            'sku', 'nombre', 'categoria', 'costo_compra',
            'precio_venta', 'exento_itbis', 'stock', 'stock_minimo',
        ]
        widgets = {
            'sku': forms.TextInput(attrs={'placeholder': 'Ej. L006'}),
            'nombre': forms.TextInput(attrs={'placeholder': 'Nombre del producto'}),
            'costo_compra': forms.NumberInput(attrs={'step': '0.01'}),
            'precio_venta': forms.NumberInput(attrs={'step': '0.01'}),
        }



class CrearUsuarioForm(UserCreationForm):
    """
    Crea un usuario nuevo usando el mecanismo nativo de Django
    (UserCreationForm ya valida y guarda la contraseña con hash,
    nunca en texto plano). Se le agrega un campo de grupo/rol.
    """
    grupo = forms.ModelChoiceField(
        queryset=Group.objects.none(),
        label='Rol',
        required=True,
    )

    def __init__(self, *args, **kwargs):
        _asegurar_roles_base()
        super().__init__(*args, **kwargs)
        # Incluye el rol Administrador y cualquier rol personalizado
        # creado desde Roles y Permisos, para que puedan asignarse a
        # un usuario igual que el rol protegido.
        self.fields['grupo'].queryset = Group.objects.all().order_by('name')

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username',)
        widgets = {
            'username': forms.TextInput(attrs={'placeholder': 'Nombre de usuario'}),
        }


class EditarUsuarioForm(forms.ModelForm):
    """
    Edita los datos básicos de un usuario y su rol (grupo). No
    incluye la contraseña ni el estado activo/inactivo, que se
    manejan por separado con sus propios formularios/acciones.
    """
    grupo = forms.ModelChoiceField(
        queryset=Group.objects.none(),
        label='Rol',
        required=True,
    )

    def __init__(self, *args, **kwargs):
        _asegurar_roles_base()
        super().__init__(*args, **kwargs)
        # Igual que en CrearUsuarioForm: cualquier rol (base o
        # personalizado) puede asignarse a un usuario existente.
        self.fields['grupo'].queryset = Group.objects.all().order_by('name')

    class Meta:
        model = User
        fields = ('username',)
        widgets = {
            'username': forms.TextInput(attrs={'placeholder': 'Nombre de usuario'}),
        }


class RolForm(forms.ModelForm):
    """
    Formulario mínimo para crear/renombrar un rol (Group). La selección
    de permisos NO se maneja aquí como ModelMultipleChoiceField: la
    vista la procesa directamente desde request.POST.getlist('permisos')
    usando core.roles_permisos.permisos_validos_desde_ids, porque la
    interfaz necesita agrupar los checkboxes por módulo (no una lista
    plana), y esta es la forma más simple de lograrlo sin duplicar
    validación. El nombre sí usa la validación nativa de Django
    (unicidad de Group.name incluida).
    """
    class Meta:
        model = Group
        fields = ['name']
        labels = {'name': 'Nombre del rol'}
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'Ej. Supervisor de Ventas'}),
        }


class SolicitarRecuperacionForm(forms.Form):
    """Formulario público (sin login) para pedir recuperación de
    contraseña. Solo pide un identificador; nunca se le dice al
    usuario si existe o no una cuenta con ese dato (ver
    views.recuperacion_solicitar)."""
    identificador = forms.CharField(
        label='Usuario o correo electrónico',
        max_length=150,
        widget=forms.TextInput(attrs={
            'placeholder': 'Tu nombre de usuario o correo electrónico',
            'autofocus': True,
        }),
    )


# Formularios adicionales del módulo de funcionalidad.

class ProveedorForm(forms.ModelForm):
    class Meta:
        model = Proveedor
        fields = ['nombre', 'rnc', 'contacto', 'telefono', 'email', 'direccion']
        widgets = {
            'nombre': forms.TextInput(attrs={'placeholder': 'Nombre o razón social'}),
            'rnc': forms.TextInput(attrs={'placeholder': 'RNC / identificación'}),
            'contacto': forms.TextInput(attrs={'placeholder': 'Persona de contacto'}),
            'telefono': forms.TextInput(attrs={'placeholder': 'Teléfono'}),
            'email': forms.EmailInput(attrs={'placeholder': 'correo@proveedor.com'}),
            'direccion': forms.TextInput(attrs={'placeholder': 'Dirección'}),
        }


class OrdenCompraForm(forms.ModelForm):
    class Meta:
        model = OrdenCompra
        fields = ['proveedor', 'notas']
        widgets = {
            'notas': forms.Textarea(attrs={'rows': 2, 'placeholder': 'Notas (opcional)'}),
        }


DetalleOrdenCompraFormSet = forms.inlineformset_factory(
    OrdenCompra, DetalleOrdenCompra,
    fields=['producto', 'cantidad_solicitada', 'costo_unitario'],
    extra=3, can_delete=True,
    widgets={
        'cantidad_solicitada': forms.NumberInput(attrs={'min': 1}),
        'costo_unitario': forms.NumberInput(attrs={'step': '0.01', 'min': 0}),
    },
)


class CompraForm(forms.ModelForm):
    class Meta:
        model = Compra
        fields = [
            'proveedor', 'orden', 'recepcion', 'numero_factura', 'fecha',
            'subtotal', 'impuestos', 'total', 'condicion_pago',
            'fecha_vencimiento',
        ]
        widgets = {
            'numero_factura': forms.TextInput(attrs={'placeholder': 'Ej. B0100001234'}),
            'fecha': forms.DateInput(attrs={'type': 'date'}),
            'subtotal': forms.NumberInput(attrs={'step': '0.01'}),
            'impuestos': forms.NumberInput(attrs={'step': '0.01'}),
            'total': forms.NumberInput(attrs={'step': '0.01'}),
            'fecha_vencimiento': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['orden'].required = False
        self.fields['recepcion'].required = False
        self.fields['fecha_vencimiento'].required = False

    def clean(self):
        cleaned_data = super().clean()
        condicion = cleaned_data.get('condicion_pago')
        vencimiento = cleaned_data.get('fecha_vencimiento')
        if condicion == 'Credito' and not vencimiento:
            self.add_error('fecha_vencimiento', 'Indica la fecha de vencimiento para una compra a crédito.')
        if condicion == 'Contado':
            cleaned_data['fecha_vencimiento'] = None
        return cleaned_data

from .models import Cliente, Empleado, DatosEmpresa, PreferenciasNotificacion, DiaFeriado, Ausencia

class ClienteForm(forms.ModelForm):
    class Meta:
        model = Cliente
        fields = [
            'nombre', 'empresa', 'tipo', 'rnc_cedula', 'telefono', 'email',
            'direccion', 'condicion_fiscal', 'activo',
        ]
        widgets = {
            'nombre': forms.TextInput(attrs={'placeholder': 'Nombre completo'}),
            'empresa': forms.TextInput(attrs={'placeholder': 'Opcional'}),
            'rnc_cedula': forms.TextInput(attrs={'placeholder': 'RNC o Cédula'}),
            'telefono': forms.TextInput(attrs={'placeholder': 'Ej. 809-555-1234'}),
            'email': forms.EmailInput(attrs={'placeholder': 'correo@ejemplo.com'}),
            'direccion': forms.TextInput(attrs={'placeholder': 'Dirección'}),
        }


class EmpleadoForm(forms.ModelForm):
    class Meta:
        model = Empleado
        fields = [
            'nombre', 'puesto', 'departamento', 'area', 'telefono', 'fecha_ingreso',
            'salario', 'estado', 'tipo_empleado', 'tipo_nomina', 'nivel_academico',
            'horas_extra_mes', 'dias_vacaciones_tomados',
        ]
        widgets = {
            'nombre': forms.TextInput(attrs={'placeholder':'Nombre completo'}),
            'puesto': forms.TextInput(attrs={'placeholder':'Ej. Vendedor, Contador...'}),
            'area': forms.TextInput(attrs={'placeholder':'Ej. Ventas corporativas, Tesorería...'}),
            'telefono': forms.TextInput(attrs={'placeholder':'Ej. 809-555-1234'}),
            'fecha_ingreso': forms.DateInput(attrs={'type':'date'}),
            'salario': forms.NumberInput(attrs={'step':'0.01'}),
            'horas_extra_mes': forms.NumberInput(attrs={'step':'0.01', 'min':'0'}),
            'dias_vacaciones_tomados': forms.NumberInput(attrs={'min':'0'}),
        }



class DiaFeriadoForm(forms.ModelForm):
    class Meta:
        model = DiaFeriado
        fields = ['fecha', 'nombre']
        widgets = {
            'fecha': forms.DateInput(attrs={'type': 'date'}),
            'nombre': forms.TextInput(attrs={'placeholder': 'Ej. Día de la Independencia'}),
        }

FeriadoForm = DiaFeriadoForm


class AusenciaForm(forms.ModelForm):
    class Meta:
        model = Ausencia
        fields = ['empleado', 'fecha', 'justificada', 'motivo']
        widgets = {
            'fecha': forms.DateInput(attrs={'type': 'date'}),
            'motivo': forms.TextInput(attrs={'placeholder': 'Motivo de la ausencia (opcional)'}),
        }


class PerfilForm(forms.ModelForm):
    telefono = forms.CharField(required=False, widget=forms.TextInput(attrs={'placeholder':'Ej. 809-555-1234'}))
    class Meta:
        model = User
        fields = ['first_name','last_name','email']
        widgets = {'first_name':forms.TextInput(attrs={'placeholder':'Nombre'}), 'last_name':forms.TextInput(attrs={'placeholder':'Apellido'}), 'email':forms.EmailInput(attrs={'placeholder':'correo@ejemplo.com'})}

class DatosEmpresaForm(forms.ModelForm):
    class Meta:
        model = DatosEmpresa
        fields = [
            'nombre_comercial', 'rnc', 'direccion', 'telefono', 'email',
            'moneda', 'itbs_porcentaje', 'dias_credito', 'isr_ano_anterior',
            'afp_porcentaje', 'sfs_porcentaje', 'dias_mes_calculo', 'factor_hora_extra',
            'porcentaje_riesgo_laboral',
        ]
        widgets = {
            'nombre_comercial': forms.TextInput(attrs={'placeholder':'Ej. Cromf Finanzas'}),
            'rnc': forms.TextInput(attrs={'placeholder':'Ej. 130-45678-2'}),
            'direccion': forms.TextInput(attrs={'placeholder':'Dirección completa'}),
            'telefono': forms.TextInput(attrs={'placeholder':'Ej. 809-555-0000'}),
            'email': forms.EmailInput(attrs={'placeholder':'empresa@ejemplo.com'}),
            'itbs_porcentaje': forms.NumberInput(attrs={'step':'0.01'}),
            'dias_credito': forms.NumberInput(attrs={'min':'0'}),
            'isr_ano_anterior': forms.NumberInput(attrs={'step':'0.01', 'min':'0', 'placeholder':'ISR liquidado en la última declaración anual'}),
            'afp_porcentaje': forms.NumberInput(attrs={'step':'0.01', 'min':'0'}),
            'sfs_porcentaje': forms.NumberInput(attrs={'step':'0.01', 'min':'0'}),
            'dias_mes_calculo': forms.NumberInput(attrs={'step':'0.01', 'min':'1'}),
            'factor_hora_extra': forms.NumberInput(attrs={'step':'0.01', 'min':'0'}),
            'porcentaje_riesgo_laboral': forms.NumberInput(attrs={
                'step':'0.01', 'min':'1.10', 'max':'1.40', 'placeholder':'Entre 1.10 y 1.40'
            }),
        }


class PreferenciasNotificacionForm(forms.ModelForm):
    class Meta:
        model = PreferenciasNotificacion
        fields = ['alertas_stock_bajo','recomendaciones_ia','resumen_diario_correo','nuevas_ventas']


class PedidoClienteForm(forms.ModelForm):
    class Meta:
        model = PedidoCliente
        fields = ['cliente', 'fecha_entrega', 'fecha_vigencia', 'observaciones']
        widgets = {
            'fecha_entrega': forms.DateInput(attrs={'type': 'date'}),
            'fecha_vigencia': forms.DateInput(attrs={'type': 'date'}),
            'observaciones': forms.Textarea(attrs={'rows': 2, 'placeholder': 'Notas del pedido (opcional)'}),
        }



DetallePedidoClienteFormSet = forms.inlineformset_factory(
    PedidoCliente, DetallePedidoCliente,
    fields=['producto', 'cantidad', 'precio_unitario'],
    extra=3, can_delete=True,
    widgets={
        'cantidad': forms.NumberInput(attrs={'min': 1}),
        'precio_unitario': forms.NumberInput(attrs={'step': '0.01', 'min': 0}),
    },
)

class CobroForm(forms.ModelForm):
    cuenta_financiera = forms.ModelChoiceField(
        queryset=CuentaBancaria.objects.none(), required=False,
        label='Cuenta / destino',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['cuenta_financiera'].queryset = CuentaBancaria.objects.filter(activa=True)

    class Meta:
        model = Cobro
        fields = ['monto', 'fecha', 'metodo_pago', 'referencia', 'observacion']
        widgets = {
            'monto': forms.NumberInput(attrs={'step': '0.01', 'min': '0.01'}),
            'fecha': forms.DateInput(attrs={'type': 'date'}),
            'referencia': forms.TextInput(attrs={'placeholder': 'Ej. núm. de transferencia (opcional)'}),
            'observacion': forms.TextInput(attrs={'placeholder': 'Opcional'}),
        }

    def clean_monto(self):
        monto = self.cleaned_data['monto']
        if monto <= 0:
            raise forms.ValidationError('El monto debe ser mayor que cero.')
        return monto


class PagoCompraForm(forms.ModelForm):
    cuenta_financiera = forms.ModelChoiceField(
        queryset=CuentaBancaria.objects.none(), required=False,
        label='Cuenta / origen',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['cuenta_financiera'].queryset = CuentaBancaria.objects.filter(activa=True)

    class Meta:
        model = PagoCompra
        fields = ['monto', 'fecha', 'metodo_pago', 'referencia', 'observacion']
        widgets = {
            'monto': forms.NumberInput(attrs={'step': '0.01', 'min': '0.01'}),
            'fecha': forms.DateInput(attrs={'type': 'date'}),
            'referencia': forms.TextInput(attrs={'placeholder': 'Ej. núm. de transferencia (opcional)'}),
            'observacion': forms.TextInput(attrs={'placeholder': 'Opcional'}),
        }

    def clean_monto(self):
        monto = self.cleaned_data['monto']
        if monto <= 0:
            raise forms.ValidationError('El monto debe ser mayor que cero.')
        return monto


class CuentaBancariaForm(forms.ModelForm):
    class Meta:
        model = CuentaBancaria
        fields = ['nombre', 'tipo', 'banco', 'numero_cuenta']
        widgets = {
            'nombre': forms.TextInput(attrs={'placeholder': 'Ej. Caja General, Banco Principal...'}),
            'banco': forms.TextInput(attrs={'placeholder': 'Banco (opcional)'}),
            'numero_cuenta': forms.TextInput(attrs={'placeholder': 'Número de cuenta (opcional)'}),
        }

