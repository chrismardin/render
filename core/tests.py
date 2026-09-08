import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth.models import Group, Permission, User
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Cliente, DetalleVenta, MovimientoFinanciero, MovimientoStock, Producto, Venta,
    Proveedor, OrdenCompra, DetalleOrdenCompra, Compra,
    PedidoCliente, DetallePedidoCliente, DatosEmpresa,
    CuentaPorCobrar, Cobro, PagoCompra, Empleado, Ausencia, DiaFeriado, Nomina,
    DetalleNomina,
)
from . import servicios_nomina as nomina_servicio
from .proteccion_administrador import CODENAMES_CRITICOS_ADMINISTRADOR


class SistemaBaseTest(TestCase):
    def setUp(self):
        grupo = self.crear_rol('Administrador')
        self.user = User.objects.create_user(username="admin", password="Pass12345")
        self.user.groups.add(grupo)
        self.cliente = Cliente.objects.create(nombre="Cliente Test")
        self.producto = Producto.objects.create(
            sku="T001", nombre="Producto Test", categoria="Laptops",
            costo_compra="100.00", precio_venta="200.00",
            stock=10, stock_minimo=2,
        )
        self.client.login(username="admin", password="Pass12345")

    @staticmethod
    def crear_rol(nombre, *codenames):
        grupo, _ = Group.objects.get_or_create(name=nombre)
        permisos = Permission.objects.filter(content_type__app_label='core')
        if codenames:
            permisos = permisos.filter(codename__in=codenames)
        grupo.permissions.set(permisos)
        return grupo


class PaginasPrincipalesTest(SistemaBaseTest):
    def test_paginas_principales(self):
        for url in ["/", "/productos/", "/ventas/", "/finanzas/", "/inventario/",
                    "/proyecciones/", "/clientes/", "/empleados/", "/reportes/",
                    "/configuracion/", "/usuarios/"]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)


class VentaRealTest(SistemaBaseTest):
    def test_procesar_venta_descuenta_stock_y_guarda_relaciones(self):
        response = self.client.post(
            reverse("procesar_venta"),
            data=json.dumps({
                "productos": [{"sku": "T001", "cantidad": 3}],
                "cliente_id": self.cliente.id,
                "medio_pago": "Tarjeta",
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 7)
        self.assertEqual(Venta.objects.count(), 1)
        self.assertEqual(DetalleVenta.objects.count(), 1)
        self.assertEqual(MovimientoStock.objects.filter(tipo="Salida", cantidad=3).count(), 1)
        self.assertEqual(MovimientoFinanciero.objects.filter(categoria="Ventas").count(), 1)


class CrudBasicoTest(SistemaBaseTest):
    def test_crear_cliente(self):
        response = self.client.post("/clientes/", {
            "accion": "crear", "nombre": "Nuevo Cliente",
            "empresa": "Empresa", "telefono": "809-000-0000", "tipo": "Publico",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Cliente.objects.filter(nombre="Nuevo Cliente").exists())


class AutorizacionDinamicaTest(SistemaBaseTest):
    def test_administrador_accede_a_pedidos(self):
        response = self.client.get(reverse('pedidos_clientes_lista'))
        self.assertEqual(response.status_code, 200)

    def test_usuario_sin_permiso_es_rechazado(self):
        usuario = User.objects.create_user(username='sin_permiso', password='Pass12345')
        self.client.force_login(usuario)
        response = self.client.get(reverse('clientes'))
        self.assertEqual(response.status_code, 302)

    def test_rol_personalizado_recibe_solo_sus_permisos(self):
        grupo = self.crear_rol('Supervisor de Ventas', 'ver_ventas')
        usuario = User.objects.create_user(username='supervisor', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        permitido = self.client.get(reverse('ventas'))
        denegado = self.client.get(reverse('clientes'))

        self.assertEqual(permitido.status_code, 200)
        self.assertEqual(denegado.status_code, 302)

    def test_no_se_puede_desactivar_al_ultimo_administrador(self):
        self.user.delete()
        actor_group = self.crear_rol('Operador de Usuarios', 'ver_usuarios')
        actor = User.objects.create_user(username='operator', password='Pass12345')
        actor.groups.add(actor_group)
        administrador = User.objects.create_user(username='sole_admin', password='Pass12345')
        administrador.groups.add(Group.objects.get(name='Administrador'))

        self.client.force_login(actor)
        response = self.client.post(
            reverse('usuarios_desactivar', args=[administrador.pk]),
        )

        administrador.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(administrador.is_active)


class ComprasTest(SistemaBaseTest):
    """Compras: la Orden NO afecta inventario; la Recepción sí, respetando
    la cantidad pendiente (sin sobre-recepción)."""

    def setUp(self):
        super().setUp()
        self.proveedor = Proveedor.objects.create(nombre="Proveedor Test")
        self.orden = OrdenCompra.objects.create(proveedor=self.proveedor, creado_por=self.user)
        self.detalle = DetalleOrdenCompra.objects.create(
            orden=self.orden, producto=self.producto,
            cantidad_solicitada=5, costo_unitario="80.00",
        )

    def test_crear_orden_de_compra_no_modifica_stock(self):
        stock_antes = self.producto.stock
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, stock_antes)
        self.assertEqual(MovimientoStock.objects.count(), 0)

    def test_recepcion_si_modifica_stock(self):
        stock_antes = self.producto.stock
        response = self.client.post(
            reverse('compras_recepcion_nueva', args=[self.orden.pk]),
            {f'cantidad_{self.detalle.pk}': '3', 'notas': ''},
        )
        self.assertEqual(response.status_code, 302)
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, stock_antes + 3)
        self.assertEqual(
            MovimientoStock.objects.filter(tipo='Entrada', producto=self.producto).count(), 1
        )
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, 'Parcialmente recibida')

    def test_no_se_puede_sobre_recibir(self):
        stock_antes = self.producto.stock
        response = self.client.post(
            reverse('compras_recepcion_nueva', args=[self.orden.pk]),
            # Se solicitaron 5; se intenta recibir 999.
            {f'cantidad_{self.detalle.pk}': '999', 'notas': ''},
        )
        self.assertEqual(response.status_code, 302)
        self.producto.refresh_from_db()
        # La cantidad recibida queda acotada a lo solicitado (5), no a
        # lo que se intentó recibir (999).
        self.assertEqual(self.producto.stock, stock_antes + 5)
        self.detalle.refresh_from_db()
        self.assertEqual(self.detalle.cantidad_pendiente, 0)
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, 'Recibida')


class PedidosAutorizacionTest(SistemaBaseTest):
    """El acceso a Pedidos de Clientes depende del permiso Django, no
    del rol ni de que el enlace esté oculto en el HTML."""

    def test_usuario_con_permiso_accede_a_pedidos(self):
        grupo = self.crear_rol('Con Permiso Pedidos', 'ver_pedidos_clientes')
        usuario = User.objects.create_user(username='con_pedidos', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        response = self.client.get(reverse('pedidos_clientes_lista'))
        self.assertEqual(response.status_code, 200)

    def test_usuario_sin_permiso_recibe_403_por_acceso_directo(self):
        usuario = User.objects.create_user(username='sin_pedidos', password='Pass12345')
        self.client.force_login(usuario)

        response = self.client.get(reverse('pedidos_clientes_lista'))
        self.assertEqual(response.status_code, 403)


class ProteccionAdministradorTest(SistemaBaseTest):
    """Amplía la protección del último Administrador: eliminación,
    cambio de rol, eliminación/permisos del grupo, y el caso de dos
    administradores."""

    def setUp(self):
        super().setUp()
        # self.user (creado por SistemaBaseTest) queda como el único
        # Administrador para la mayoría de estos tests.
        self.grupo_admin = Group.objects.get(name='Administrador')

    def test_no_se_puede_eliminar_al_ultimo_administrador_via_admin(self):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('admin:auth_user_delete', args=[self.user.pk]),
        )
        # El admin no debe completar el borrado: se queda en la página
        # (200, "no tienes permiso") en vez de redirigir tras borrar.
        self.assertNotEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_no_se_puede_quitarle_el_rol_administrador_al_ultimo_admin_via_editar(self):
        otro_grupo = self.crear_rol('Rol Sin Privilegios')
        response = self.client.post(
            reverse('usuarios_editar', args=[self.user.pk]),
            {'username': self.user.username, 'grupo': otro_grupo.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.groups.filter(name='Administrador').exists())

    def test_no_se_puede_eliminar_el_grupo_administrador_via_roles_eliminar(self):
        response = self.client.post(
            reverse('roles_eliminar', args=[self.grupo_admin.pk]),
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Group.objects.filter(pk=self.grupo_admin.pk).exists())

    def test_no_se_puede_eliminar_el_grupo_administrador_via_admin(self):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('admin:auth_group_delete', args=[self.grupo_admin.pk]),
        )
        self.assertNotEqual(response.status_code, 302)
        self.assertTrue(Group.objects.filter(pk=self.grupo_admin.pk).exists())

    def test_no_se_puede_dejar_al_administrador_sin_permisos_criticos_via_roles_actualizar(self):
        permisos_sin_criticos = Permission.objects.filter(
            content_type__app_label='core'
        ).exclude(codename__in=CODENAMES_CRITICOS_ADMINISTRADOR)
        permisos_antes = set(self.grupo_admin.permissions.values_list('pk', flat=True))

        response = self.client.post(
            reverse('roles_actualizar', args=[self.grupo_admin.pk]),
            {'permisos': [p.pk for p in permisos_sin_criticos]},
        )

        self.assertEqual(response.status_code, 302)
        self.grupo_admin.refresh_from_db()
        permisos_despues = set(self.grupo_admin.permissions.values_list('pk', flat=True))
        # No se aplicó el cambio: sigue teniendo los permisos críticos.
        self.assertEqual(permisos_antes, permisos_despues)
        codenames_actuales = set(
            self.grupo_admin.permissions.values_list('codename', flat=True)
        )
        self.assertTrue(CODENAMES_CRITICOS_ADMINISTRADOR.issubset(codenames_actuales))

    def test_no_se_puede_dejar_al_administrador_sin_permisos_criticos_via_admin(self):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)

        permisos_sin_criticos = list(
            Permission.objects.filter(content_type__app_label='core')
            .exclude(codename__in=CODENAMES_CRITICOS_ADMINISTRADOR)
            .values_list('pk', flat=True)
        )

        response = self.client.post(
            reverse('admin:auth_group_change', args=[self.grupo_admin.pk]),
            {'name': 'Administrador', 'permissions': permisos_sin_criticos},
        )
        # El formulario del admin debe rechazar el cambio (200, con
        # errores) en vez de guardarlo (que redirigiría con 302).
        self.assertEqual(response.status_code, 200)
        self.grupo_admin.refresh_from_db()
        codenames_actuales = set(
            self.grupo_admin.permissions.values_list('codename', flat=True)
        )
        self.assertTrue(CODENAMES_CRITICOS_ADMINISTRADOR.issubset(codenames_actuales))

    def test_con_dos_administradores_se_puede_desactivar_uno(self):
        segundo_admin = User.objects.create_user(username='segundo_admin', password='Pass12345')
        segundo_admin.groups.add(self.grupo_admin)

        response = self.client.post(
            reverse('usuarios_desactivar', args=[segundo_admin.pk]),
        )
        self.assertEqual(response.status_code, 302)
        segundo_admin.refresh_from_db()
        self.assertFalse(segundo_admin.is_active)
        # El primer administrador sigue activo y el sistema conserva
        # al menos uno.
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)


class PedidoClienteDespachoTest(SistemaBaseTest):
    """Flujo Cliente -> PedidoCliente -> Venta (despacho parcial/total),
    sin tocar la lógica de Ventas directas ya existente."""

    def setUp(self):
        super().setUp()
        self.pedido = PedidoCliente.objects.create(cliente=self.cliente, estado='Pendiente', creado_por=self.user)
        self.detalle = DetallePedidoCliente.objects.create(
            pedido=self.pedido, producto=self.producto, cantidad=10, precio_unitario='200.00',
        )

    def test_crear_pedido_no_afecta_stock_ni_finanzas(self):
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 10)
        self.assertEqual(MovimientoStock.objects.count(), 0)
        self.assertEqual(MovimientoFinanciero.objects.count(), 0)

    def test_no_se_puede_despachar_un_pedido_no_confirmado(self):
        response = self.client.post(
            reverse('pedidos_clientes_despachar', args=[self.pedido.pk]),
            {f'cantidad_{self.detalle.pk}': '5'},
        )
        self.assertEqual(response.status_code, 302)
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 10)
        self.assertEqual(Venta.objects.count(), 0)

    def test_despacho_parcial_genera_venta_y_deja_pedido_confirmado(self):
        self.pedido.estado = 'Confirmado'
        self.pedido.save()

        response = self.client.post(
            reverse('pedidos_clientes_despachar', args=[self.pedido.pk]),
            {f'cantidad_{self.detalle.pk}': '4', 'metodo_pago': 'Efectivo'},
        )
        self.assertEqual(response.status_code, 302)

        self.assertEqual(Venta.objects.count(), 1)
        venta = Venta.objects.get()
        self.assertEqual(venta.pedido_id, self.pedido.pk)
        self.assertEqual(venta.cliente_id, self.cliente.pk)

        detalle_venta = venta.detalles.get()
        self.assertEqual(detalle_venta.detalle_pedido_id, self.detalle.pk)
        self.assertEqual(detalle_venta.cantidad, 4)
        self.detalle.refresh_from_db()
        self.assertEqual(detalle_venta.precio_unitario, self.detalle.precio_unitario)

        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 6)
        self.assertEqual(
            MovimientoStock.objects.filter(tipo='Salida', producto=self.producto).count(), 1
        )

        movimiento = MovimientoFinanciero.objects.get()
        self.assertEqual(movimiento.venta_id, venta.pk)
        self.assertEqual(movimiento.tipo, 'Ingreso')

        self.detalle.refresh_from_db()
        self.assertEqual(self.detalle.cantidad_facturada, 4)
        self.assertEqual(self.detalle.cantidad_pendiente, 6)

        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, 'Confirmado')

    def test_segundo_despacho_completa_el_pedido(self):
        self.pedido.estado = 'Confirmado'
        self.pedido.save()

        self.client.post(
            reverse('pedidos_clientes_despachar', args=[self.pedido.pk]),
            {f'cantidad_{self.detalle.pk}': '4'},
        )
        self.client.post(
            reverse('pedidos_clientes_despachar', args=[self.pedido.pk]),
            {f'cantidad_{self.detalle.pk}': '6'},
        )

        self.assertEqual(Venta.objects.count(), 2)
        self.detalle.refresh_from_db()
        self.assertEqual(self.detalle.cantidad_facturada, 10)
        self.assertEqual(self.detalle.cantidad_pendiente, 0)

        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, 'Completado')

        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 0)

    def test_no_se_puede_sobre_despachar(self):
        self.pedido.estado = 'Confirmado'
        self.pedido.save()

        # Se solicitaron 10; se intenta despachar 999.
        response = self.client.post(
            reverse('pedidos_clientes_despachar', args=[self.pedido.pk]),
            {f'cantidad_{self.detalle.pk}': '999'},
        )
        self.assertEqual(response.status_code, 302)

        self.detalle.refresh_from_db()
        # Queda acotado a lo solicitado (10), no a lo que se intentó (999).
        self.assertEqual(self.detalle.cantidad_facturada, 10)
        self.assertEqual(self.detalle.cantidad_pendiente, 0)

        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 0)

    def test_precio_del_pedido_no_se_ve_afectado_por_cambio_posterior_en_producto(self):
        self.pedido.estado = 'Confirmado'
        self.pedido.save()

        self.producto.precio_venta = '350.00'
        self.producto.save(update_fields=['precio_venta'])

        self.client.post(
            reverse('pedidos_clientes_despachar', args=[self.pedido.pk]),
            {f'cantidad_{self.detalle.pk}': '2'},
        )

        venta = Venta.objects.get()
        detalle_venta = venta.detalles.get()
        # Se usó el precio acordado en el pedido (200.00), no el nuevo
        # precio_venta del producto (350.00).
        self.assertEqual(detalle_venta.precio_unitario, Decimal('200.00'))

    def test_venta_directa_sin_pedido_sigue_funcionando(self):
        response = self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({
                'productos': [{'sku': self.producto.sku, 'cantidad': 2}],
                'cliente_id': self.cliente.id,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        venta = Venta.objects.exclude(pk=None).latest('id')
        self.assertIsNone(venta.pedido)


class TrazabilidadFinancieraTest(SistemaBaseTest):
    """MovimientoFinanciero.venta/.compra: los movimientos generados
    por el sistema quedan enlazados de verdad (no solo por texto libre
    en 'categoria'), y el formulario manual no puede fingir ser uno de
    ellos."""

    def test_venta_directa_genera_movimiento_ligado_a_la_venta(self):
        self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({
                'productos': [{'sku': self.producto.sku, 'cantidad': 1}],
                'cliente_id': self.cliente.id,
            }),
            content_type='application/json',
        )
        venta = Venta.objects.get()
        movimiento = MovimientoFinanciero.objects.get()
        self.assertEqual(movimiento.venta_id, venta.pk)
        self.assertIsNone(movimiento.compra_id)

    def test_compra_contado_genera_movimiento_ligado_a_la_compra(self):
        proveedor = Proveedor.objects.create(nombre='Proveedor Test')
        response = self.client.post(
            reverse('compras_lista'),
            {
                'proveedor': proveedor.pk, 'numero_factura': 'F-001',
                'fecha': '2026-01-15', 'subtotal': '100.00',
                'impuestos': '18.00', 'total': '118.00',
                'condicion_pago': 'Contado',
            },
        )
        self.assertEqual(response.status_code, 302)
        compra = Compra.objects.get()
        movimiento = MovimientoFinanciero.objects.get()
        self.assertEqual(movimiento.compra_id, compra.pk)
        self.assertIsNone(movimiento.venta_id)

    def test_pago_de_compra_a_credito_genera_movimiento_ligado_a_la_compra(self):
        proveedor = Proveedor.objects.create(nombre='Proveedor Test')
        compra = Compra.objects.create(
            proveedor=proveedor, numero_factura='F-002', fecha='2026-01-15',
            subtotal='100.00', impuestos='18.00', total='118.00',
            condicion_pago='Credito', estado_pago='Pendiente',
        )
        response = self.client.post(
            reverse('compras_registrar_pago', args=[compra.pk]),
            {'monto': '50.00'},
        )
        self.assertEqual(response.status_code, 302)
        movimiento = MovimientoFinanciero.objects.get()
        self.assertEqual(movimiento.compra_id, compra.pk)

    def test_finanzas_no_permite_ingreso_manual(self):
        response = self.client.post(
            reverse('finanzas'),
            {
                'tipo': 'Ingreso', 'fecha': '2026-01-15', 'categoria': 'Otro ingreso',
                'cliente_proveedor': 'Alguien', 'monto': '500.00', 'medio_pago': 'Efectivo',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(MovimientoFinanciero.objects.count(), 0)

    def test_formulario_manual_no_permite_categoria_compras(self):
        response = self.client.post(
            reverse('finanzas') + '?tab=gastos',
            {
                'accion': 'registrar_gasto', 'fecha': '2026-01-15', 'categoria': 'compras',
                'cliente_proveedor': 'Alguien', 'monto': '500.00', 'medio_pago': 'Efectivo',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MovimientoFinanciero.objects.count(), 0)

    def test_formulario_manual_permite_gasto_operativo(self):
        response = self.client.post(
            reverse('finanzas') + '?tab=gastos',
            {
                'accion': 'registrar_gasto', 'fecha': '2026-01-15', 'categoria': 'Alquiler',
                'cliente_proveedor': 'Casero', 'monto': '500.00', 'medio_pago': 'Efectivo',
            },
        )
        self.assertEqual(response.status_code, 302)
        movimiento = MovimientoFinanciero.objects.get()
        self.assertEqual(movimiento.tipo, 'Gasto')
        self.assertIsNone(movimiento.venta_id)
        self.assertIsNone(movimiento.compra_id)


class HistorialClienteTest(SistemaBaseTest):
    """Ficha del cliente: el historial sale de Venta real; los pedidos
    pendientes se muestran aparte y nunca cuentan como compra."""

    def test_pedido_pendiente_no_aparece_como_historial_de_compras(self):
        pedido = PedidoCliente.objects.create(cliente=self.cliente, estado='Confirmado', creado_por=self.user)
        DetallePedidoCliente.objects.create(
            pedido=pedido, producto=self.producto, cantidad=5, precio_unitario='200.00',
        )

        response = self.client.get(reverse('clientes_detalle', args=[self.cliente.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['ventas']), [])
        self.assertEqual(list(response.context['pedidos_pendientes']), [pedido])

    def test_venta_real_aparece_en_el_historial_y_pedido_completado_no_esta_pendiente(self):
        pedido = PedidoCliente.objects.create(cliente=self.cliente, estado='Confirmado', creado_por=self.user)
        detalle = DetallePedidoCliente.objects.create(
            pedido=pedido, producto=self.producto, cantidad=3, precio_unitario='200.00',
        )
        self.client.post(
            reverse('pedidos_clientes_despachar', args=[pedido.pk]),
            {f'cantidad_{detalle.pk}': '3'},
        )

        response = self.client.get(reverse('clientes_detalle', args=[self.cliente.pk]))
        ventas = list(response.context['ventas'])
        self.assertEqual(len(ventas), 1)
        self.assertEqual(ventas[0].pedido_id, pedido.pk)
        # El pedido ya quedó Completado (0 pendiente): no debe listarse
        # como pedido pendiente.
        self.assertEqual(list(response.context['pedidos_pendientes']), [])

    def test_historial_no_incluye_ventas_de_otro_cliente(self):
        otro_cliente = Cliente.objects.create(nombre='Otro Cliente')
        Venta.objects.create(cliente=otro_cliente, subtotal='100', itbs='18', total='118')

        response = self.client.get(reverse('clientes_detalle', args=[self.cliente.pk]))
        self.assertEqual(list(response.context['ventas']), [])


class ItbisSinHardcodearTest(SistemaBaseTest):
    """La tasa de ITBIS del frontend de Ventas debe venir de
    DatosEmpresa.itbs_porcentaje, no estar escrita en el JS ni en el
    template."""

    def test_vista_ventas_expone_la_tasa_configurada(self):
        DatosEmpresa.objects.update_or_create(pk=1, defaults={'itbs_porcentaje': '16.00'})

        response = self.client.get(reverse('ventas'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['itbs_porcentaje_json'], 16.0)
        self.assertContains(response, '"itbs-porcentaje"')

    def test_no_queda_0_18_hardcodeado_en_ventas_js(self):
        with open('core/static/js/ventas.js', encoding='utf-8') as f:
            contenido = f.read()
        self.assertNotIn('0.18', contenido)


class VentasCreditoCuentasPorCobrarTest(SistemaBaseTest):
    def procesar_credito(self, cantidad=1):
        return self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({
                'productos': [{'sku': self.producto.sku, 'cantidad': cantidad}],
                'cliente_id': self.cliente.id,
                'medio_pago': 'credito',
            }),
            content_type='application/json',
        )

    def test_venta_credito_genera_cuenta_por_cobrar_sin_ingreso_inmediato(self):
        response = self.procesar_credito()
        self.assertEqual(response.status_code, 200)
        venta = Venta.objects.get()
        self.assertEqual(venta.metodo_pago, 'Credito')
        self.assertEqual(MovimientoFinanciero.objects.filter(venta=venta, tipo='Ingreso').count(), 0)
        self.assertEqual(venta.saldo_pendiente, venta.total)

        contabilidad = self.client.get(reverse('contabilidad'))
        self.assertEqual(contabilidad.status_code, 200)
        self.assertEqual(contabilidad.context['cuentas_por_cobrar'], venta.total)

    def test_cobro_parcial_reduce_cuenta_por_cobrar_y_crea_ingreso(self):
        self.procesar_credito()
        venta = Venta.objects.get()
        monto = Decimal('50.00')

        response = self.client.post(
            reverse('registrar_cobro_venta', args=[venta.pk]),
            {'monto': str(monto), 'medio_pago': 'Efectivo'},
        )
        self.assertEqual(response.status_code, 302)

        movimiento = MovimientoFinanciero.objects.get(venta=venta, categoria='Cobro de Venta')
        self.assertEqual(movimiento.monto, monto)
        self.assertEqual(venta.monto_pagado, monto)
        self.assertEqual(venta.saldo_pendiente, venta.total - monto)

        contabilidad = self.client.get(reverse('contabilidad'))
        self.assertEqual(
            contabilidad.context['cuentas_por_cobrar'],
            venta.total - monto,
        )

    def test_venta_credito_exige_cliente_registrado(self):
        response = self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({
                'productos': [{'sku': self.producto.sku, 'cantidad': 1}],
                'medio_pago': 'credito',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Venta.objects.count(), 0)
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 10)

    def test_dashboard_separa_ventas_realizadas_de_ingresos_cobrados(self):
        self.procesar_credito()
        venta = Venta.objects.get()
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_ventas_mes'], venta.total)
        self.assertEqual(response.context['total_ingresos'], Decimal('0'))

    def test_cliente_muestra_saldo_por_cobrar(self):
        self.procesar_credito()
        venta = Venta.objects.get()
        response = self.client.get(reverse('clientes_detalle', args=[self.cliente.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['saldo_por_cobrar'], venta.total)


# Integrado desde Mónica actual: pruebas de regresión RRHH/Nómina.
class RRHHEmpleadosTest(SistemaBaseTest):
    def setUp(self):
        super().setUp()
        self.empleado = Empleado.objects.create(
            nombre='Juan Perez', puesto='Vendedor', departamento='Ventas',
            fecha_ingreso=timezone.now().date(), salario=Decimal('30000.00'),
        )
        self.datos_empleado_post = {
            'nombre': 'Nuevo Empleado', 'puesto': 'Cajero',
            'departamento': 'Ventas', 'area': 'Caja principal',
            'telefono': '', 'fecha_ingreso': timezone.now().date(),
            'salario': '20000', 'estado': 'Activo',
            'tipo_empleado': 'Fijo', 'nivel_academico': '',
            'horas_extra_mes': '0', 'dias_vacaciones_tomados': '0',
        }

    def test_usuario_sin_permiso_no_puede_ver_empleados(self):
        usuario = User.objects.create_user(username='sin_permiso_emp', password='Pass12345')
        self.client.force_login(usuario)
        response = self.client.get(reverse('empleados'))
        # requiere_permiso() redirige (no 403) para no romper el
        # comportamiento visible que ya tenía antes de Fase 1/2.
        self.assertEqual(response.status_code, 302)

    def test_usuario_con_ver_pero_no_gestionar_no_puede_crear(self):
        grupo = self.crear_rol('Solo Consulta Empleados', 'ver_empleados')
        usuario = User.objects.create_user(username='consulta_emp', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        puede_ver = self.client.get(reverse('empleados'))
        self.client.post(reverse('empleados'), {'accion': 'crear', **self.datos_empleado_post})

        self.assertEqual(puede_ver.status_code, 200)
        self.assertEqual(Empleado.objects.count(), 1)  # no se creó el nuevo

    def test_usuario_con_gestionar_puede_crear_empleado(self):
        grupo = self.crear_rol('RRHH Empleados', 'ver_empleados', 'gestionar_empleados')
        usuario = User.objects.create_user(username='gestor_emp', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        response = self.client.post(reverse('empleados'), {'accion': 'crear', **self.datos_empleado_post})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Empleado.objects.count(), 2)

    def test_desactivar_empleado_cambia_estado_y_no_lo_elimina(self):
        response = self.client.post(reverse('empleados'), {
            'accion': 'desactivar', 'empleado_id': self.empleado.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.empleado.refresh_from_db()
        self.assertEqual(self.empleado.estado, 'Inactivo')
        self.assertEqual(Empleado.objects.count(), 1)

    def test_desactivar_empleado_ya_inactivo_no_falla_ni_lo_reprocesa(self):
        self.empleado.estado = 'Inactivo'
        self.empleado.save()
        response = self.client.post(reverse('empleados'), {
            'accion': 'desactivar', 'empleado_id': self.empleado.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.empleado.refresh_from_db()
        self.assertEqual(self.empleado.estado, 'Inactivo')

    def test_boton_desactivar_no_aparece_para_empleado_inactivo(self):
        self.empleado.estado = 'Inactivo'
        self.empleado.save()
        response = self.client.get(reverse('empleados'))
        self.assertNotContains(response, 'value="desactivar"')



# Integrado desde Mónica actual: pruebas de regresión RRHH/Nómina.
class RRHHAusenciasTest(SistemaBaseTest):
    def setUp(self):
        super().setUp()
        self.empleado = Empleado.objects.create(
            nombre='Ana Gomez', puesto='Contadora', departamento='Finanzas',
            fecha_ingreso=timezone.now().date(), salario=Decimal('25000.00'),
        )

    def test_usuario_sin_permiso_no_puede_ver_ausencias(self):
        usuario = User.objects.create_user(username='sin_permiso_aus', password='Pass12345')
        self.client.force_login(usuario)
        response = self.client.get(reverse('rrhh_ausencias'))
        self.assertEqual(response.status_code, 403)

    def test_usuario_con_ver_pero_no_gestionar_no_puede_registrar(self):
        grupo = self.crear_rol('Solo Consulta Ausencias', 'ver_ausencias')
        usuario = User.objects.create_user(username='consulta_aus', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        puede_ver = self.client.get(reverse('rrhh_ausencias'))
        respuesta_post = self.client.post(reverse('rrhh_ausencias'), {
            'accion': 'crear', 'empleado': self.empleado.pk,
            'fecha': timezone.now().date(), 'motivo': 'Gripe',
        })
        self.assertEqual(puede_ver.status_code, 200)
        self.assertEqual(respuesta_post.status_code, 403)
        self.assertEqual(Ausencia.objects.count(), 0)

    def test_usuario_con_gestionar_puede_registrar_ausencia(self):
        grupo = self.crear_rol('RRHH Ausencias', 'ver_ausencias', 'gestionar_ausencias')
        usuario = User.objects.create_user(username='gestor_aus', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        response = self.client.post(reverse('rrhh_ausencias'), {
            'accion': 'crear', 'empleado': self.empleado.pk,
            'fecha': timezone.now().date(), 'motivo': 'Gripe',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Ausencia.objects.count(), 1)
        self.assertEqual(Ausencia.objects.get().empleado_id, self.empleado.pk)

    def test_editar_ausencia(self):
        ausencia = Ausencia.objects.create(
            empleado=self.empleado, fecha=timezone.now().date(), motivo='Gripe',
        )
        response = self.client.post(reverse('rrhh_ausencias'), {
            'accion': 'editar', 'ausencia_id': ausencia.pk,
            'empleado': self.empleado.pk, 'fecha': ausencia.fecha,
            'motivo': 'Gripe fuerte', 'justificada': 'on',
        })
        self.assertEqual(response.status_code, 302)
        ausencia.refresh_from_db()
        self.assertEqual(ausencia.motivo, 'Gripe fuerte')
        self.assertTrue(ausencia.justificada)

    def test_eliminar_ausencia(self):
        ausencia = Ausencia.objects.create(empleado=self.empleado, fecha=timezone.now().date())
        response = self.client.post(reverse('rrhh_ausencias'), {
            'accion': 'eliminar', 'ausencia_id': ausencia.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Ausencia.objects.count(), 0)

    def test_empleado_con_ausencias_queda_protegido_contra_borrado_fisico(self):
        Ausencia.objects.create(empleado=self.empleado, fecha=timezone.now().date())
        from django.db.models import ProtectedError
        with self.assertRaises(ProtectedError):
            self.empleado.delete()



# Integrado desde Mónica actual: pruebas de regresión RRHH/Nómina.
class RRHHFeriadosTest(SistemaBaseTest):
    def test_usuario_sin_permiso_no_puede_ver_feriados(self):
        usuario = User.objects.create_user(username='sin_permiso_fer', password='Pass12345')
        self.client.force_login(usuario)
        response = self.client.get(reverse('rrhh_feriados'))
        self.assertEqual(response.status_code, 403)

    def test_usuario_con_ver_pero_no_gestionar_no_puede_crear(self):
        grupo = self.crear_rol('Solo Consulta Feriados', 'ver_feriados')
        usuario = User.objects.create_user(username='consulta_fer', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)
        response = self.client.post(reverse('rrhh_feriados'), {
            'accion': 'crear', 'fecha': '2026-11-06', 'nombre': 'Día de la Constitución',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(DiaFeriado.objects.count(), 0)

    def test_usuario_con_gestionar_puede_crear_feriado(self):
        grupo = self.crear_rol('RRHH Feriados', 'ver_feriados', 'gestionar_feriados')
        usuario = User.objects.create_user(username='gestor_fer', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        response = self.client.post(reverse('rrhh_feriados'), {
            'accion': 'crear', 'fecha': '2026-11-06', 'nombre': 'Día de la Constitución',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DiaFeriado.objects.count(), 1)

    def test_editar_y_eliminar_feriado(self):
        grupo = self.crear_rol('RRHH Feriados 2', 'ver_feriados', 'gestionar_feriados')
        usuario = User.objects.create_user(username='gestor_fer2', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        feriado = DiaFeriado.objects.create(fecha='2026-01-01', nombre='Año Nuevo')
        response = self.client.post(reverse('rrhh_feriados'), {
            'accion': 'editar', 'feriado_id': feriado.pk,
            'fecha': '2026-01-01', 'nombre': 'Año Nuevo (ajustado)',
        })
        self.assertEqual(response.status_code, 302)
        feriado.refresh_from_db()
        self.assertEqual(feriado.nombre, 'Año Nuevo (ajustado)')

        response = self.client.post(reverse('rrhh_feriados'), {
            'accion': 'eliminar', 'feriado_id': feriado.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DiaFeriado.objects.count(), 0)

    def test_feriados_se_consultan_desde_bd_no_hardcodeados(self):
        DiaFeriado.objects.create(fecha='2026-02-27', nombre='Día de la Independencia RD')
        response = self.client.get(reverse('rrhh_feriados'))
        self.assertContains(response, 'Día de la Independencia RD')



# Integrado desde Mónica actual: pruebas de regresión RRHH/Nómina.
class RRHHNavegacionTest(SistemaBaseTest):
    def test_rrhh_aparece_en_sidebar_con_permiso(self):
        response = self.client.get(reverse('empleados'))
        self.assertContains(response, '>RRHH<')

    def test_rrhh_no_aparece_en_sidebar_sin_ningun_permiso_rrhh(self):
        grupo = self.crear_rol('Sin RRHH', 'ver_dashboard')
        usuario = User.objects.create_user(username='sin_rrhh', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)
        response = self.client.get(reverse('dashboard'))
        self.assertNotContains(response, '>RRHH<')

    def test_ver_nomina_sin_gestionar_no_muestra_formulario_de_generar(self):
        grupo = self.crear_rol('Ver Nomina', 'ver_nomina')
        usuario = User.objects.create_user(username='ver_nom', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)
        response = self.client.get(reverse('rrhh_nomina'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Generar nómina mensual')

    def test_tabs_internos_de_rrhh_presentes_en_empleados(self):
        response = self.client.get(reverse('empleados'))
        self.assertContains(response, reverse('rrhh_ausencias'))
        self.assertContains(response, reverse('rrhh_feriados'))
        self.assertContains(response, reverse('rrhh_nomina'))



# Integrado desde Mónica actual: pruebas de regresión RRHH/Nómina.
class GeneracionNominaMensualTest(SistemaBaseTest):
    def setUp(self):
        super().setUp()
        self.empleado_activo = Empleado.objects.create(
            nombre='Carlos Mena', puesto='Analista', departamento='Finanzas',
            fecha_ingreso=date(2020, 1, 10), salario=Decimal('40000.00'),
            estado='Activo', horas_extra_mes=Decimal('10'),
        )
        self.empleado_inactivo = Empleado.objects.create(
            nombre='Ex Empleado', puesto='Analista', departamento='Finanzas',
            fecha_ingreso=date(2019, 1, 10), salario=Decimal('35000.00'),
            estado='Inactivo',
        )
        self.grupo_nomina = self.crear_rol('RRHH Nomina', 'ver_nomina', 'gestionar_nomina')
        self.usuario_nomina = User.objects.create_user(username='rrhh_nom', password='Pass12345')
        self.usuario_nomina.groups.add(self.grupo_nomina)
        self.client.force_login(self.usuario_nomina)

    def test_generar_nomina_mensual_valida(self):
        response = self.client.post(reverse('rrhh_nomina'), {'anio': 2026, 'mes': 9})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Nomina.objects.count(), 1)
        nomina = Nomina.objects.get()
        self.assertEqual(nomina.fecha_inicio, date(2026, 9, 1))
        self.assertEqual(nomina.fecha_fin, date(2026, 9, 30))
        self.assertEqual(nomina.estado, 'Pendiente de pago')

    def test_generar_nomina_rechaza_periodo_no_mensual_a_nivel_de_modelo(self):
        # No hay forma de pedir un período quincenal desde la vista
        # (solo year/month), pero el modelo mismo debe seguir
        # rechazando cualquier período que no sea un mes completo.
        nomina = Nomina(fecha_inicio=date(2026, 9, 16), fecha_fin=date(2026, 9, 30))
        with self.assertRaises(ValidationError):
            nomina.full_clean()

    def test_no_se_puede_duplicar_nomina_del_mismo_mes(self):
        nomina_servicio.generar_nomina_mensual(2026, 9, generado_por=self.usuario_nomina)
        with self.assertRaises(ValueError):
            nomina_servicio.generar_nomina_mensual(2026, 9, generado_por=self.usuario_nomina)
        self.assertEqual(Nomina.objects.count(), 1)

    def test_solo_empleados_activos_entran_en_la_nomina(self):
        nomina = nomina_servicio.generar_nomina_mensual(2026, 9, generado_por=self.usuario_nomina)
        empleados_en_detalle = set(nomina.detalles.values_list('empleado_id', flat=True))
        self.assertIn(self.empleado_activo.pk, empleados_en_detalle)
        self.assertNotIn(self.empleado_inactivo.pk, empleados_en_detalle)

    def test_detalle_nomina_calcula_horas_extra_35_por_ciento(self):
        nomina = nomina_servicio.generar_nomina_mensual(2026, 9, generado_por=self.usuario_nomina)
        detalle = nomina.detalles.get(empleado=self.empleado_activo)
        datos_empresa = DatosEmpresa.obtener()
        valor_hora_esperado = self.empleado_activo.salario / datos_empresa.dias_mes_calculo / Decimal('8')
        esperado = (valor_hora_esperado * datos_empresa.factor_hora_extra * Decimal('10')).quantize(Decimal('0.01'))
        self.assertEqual(detalle.pago_horas_extra, esperado)

    def test_detalle_nomina_calcula_afp_sfs_isr(self):
        nomina = nomina_servicio.generar_nomina_mensual(2026, 9, generado_por=self.usuario_nomina)
        detalle = nomina.detalles.get(empleado=self.empleado_activo)
        afp_esperado = nomina_servicio.calcular_afp(self.empleado_activo.salario, 2026)
        sfs_esperado = nomina_servicio.calcular_sfs(self.empleado_activo.salario, 2026)
        self.assertEqual(detalle.afp, afp_esperado)
        self.assertEqual(detalle.sfs, sfs_esperado)
        self.assertGreaterEqual(detalle.isr, Decimal('0.00'))

    def test_detalle_nomina_incluye_descuento_por_ausencia_injustificada(self):
        Ausencia.objects.create(
            empleado=self.empleado_activo, fecha=date(2026, 9, 10), justificada=False,
        )
        Ausencia.objects.create(
            empleado=self.empleado_activo, fecha=date(2026, 9, 11), justificada=True,
        )
        nomina = nomina_servicio.generar_nomina_mensual(2026, 9, generado_por=self.usuario_nomina)
        detalle = nomina.detalles.get(empleado=self.empleado_activo)
        # Solo la ausencia injustificada cuenta para el descuento
        # (decisión tomada en este paso, ver docstring del servicio).
        self.assertEqual(detalle.dias_ausencia, 1)
        self.assertGreater(detalle.descuento_ausencias, Decimal('0.00'))

    def test_salario_neto_es_bruto_menos_deducciones(self):
        nomina = nomina_servicio.generar_nomina_mensual(2026, 9, generado_por=self.usuario_nomina)
        detalle = nomina.detalles.get(empleado=self.empleado_activo)
        self.assertEqual(
            detalle.salario_neto,
            detalle.salario_bruto - detalle.total_deducciones,
        )

    def test_snapshot_historico_no_cambia_si_cambia_el_salario_despues(self):
        nomina = nomina_servicio.generar_nomina_mensual(2026, 9, generado_por=self.usuario_nomina)
        detalle = nomina.detalles.get(empleado=self.empleado_activo)
        salario_original_en_detalle = detalle.salario_base

        self.empleado_activo.salario = Decimal('99999.00')
        self.empleado_activo.save()

        detalle.refresh_from_db()
        self.assertEqual(detalle.salario_base, salario_original_en_detalle)
        self.assertNotEqual(detalle.salario_base, self.empleado_activo.salario)

    def test_generar_nomina_no_crea_movimiento_financiero(self):
        nomina_servicio.generar_nomina_mensual(2026, 9, generado_por=self.usuario_nomina)
        self.assertEqual(MovimientoFinanciero.objects.filter(nomina__isnull=False).count(), 0)

    def test_usuario_sin_gestionar_nomina_no_puede_generar(self):
        grupo_solo_ver = self.crear_rol('Solo Ver Nomina', 'ver_nomina')
        usuario = User.objects.create_user(username='solo_ver_nom', password='Pass12345')
        usuario.groups.add(grupo_solo_ver)
        self.client.force_login(usuario)
        response = self.client.post(reverse('rrhh_nomina'), {'anio': 2026, 'mes': 9})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Nomina.objects.count(), 0)

    def test_usuario_sin_ver_nomina_no_accede(self):
        usuario = User.objects.create_user(username='sin_nomina', password='Pass12345')
        self.client.force_login(usuario)
        response = self.client.get(reverse('rrhh_nomina'))
        self.assertEqual(response.status_code, 403)

    def test_condicion_de_carrera_se_traduce_en_error_controlado_sin_datos_parciales(self):
        # Simula la condición de carrera (TOCTOU) que la comprobación
        # previa (Nomina.objects.filter(...).exists()) no puede cubrir
        # por sí sola: otra solicitud ya insertó y confirmó la Nomina
        # del mismo período ENTRE la comprobación y el guardado de
        # esta llamada. Se fuerza forzando exists() a devolver False
        # aunque la fila ya exista, para llegar de verdad al
        # UniqueConstraint('fecha_inicio') de la base de datos.
        Nomina.objects.create(fecha_inicio=date(2026, 9, 1), fecha_fin=date(2026, 9, 30))

        # Se parchean dos comprobaciones previas al guardado real
        # (la exists() de la vista/servicio y la propia validación de
        # unicidad de full_clean()), simulando que ambas "no vieron"
        # todavía la fila creada por la solicitud concurrente. Así la
        # llamada llega de verdad a nomina.save(), que es donde el
        # UniqueConstraint('fecha_inicio') de la base de datos
        # detecta el conflicto real con un IntegrityError genuino.
        with patch.object(Nomina.objects, 'filter') as mock_filter, \
                patch.object(Nomina, 'full_clean', return_value=None):
            mock_filter.return_value.exists.return_value = False
            with self.assertRaises(ValueError) as ctx:
                nomina_servicio.generar_nomina_mensual(2026, 9, generado_por=self.usuario_nomina)

        self.assertIn('Ya existe una nómina generada para 09/2026', str(ctx.exception))
        # La violación de integridad no debe filtrarse como
        # IntegrityError/500: debe llegar como ValueError controlado.
        self.assertNotIsInstance(ctx.exception, IntegrityError)
        # No debe quedar ninguna Nomina ni DetalleNomina a medias:
        # transaction.atomic() debió revertir todo lo que esta
        # llamada fallida alcanzó a escribir.
        self.assertEqual(Nomina.objects.filter(fecha_inicio=date(2026, 9, 1)).count(), 1)
        self.assertEqual(DetalleNomina.objects.count(), 0)

    def test_unique_constraint_de_bd_protege_incluso_sin_pasar_por_el_servicio(self):
        # La comprobación previa del servicio es una comodidad para
        # dar un mensaje claro en el caso normal; la protección real
        # es el UniqueConstraint del modelo. Este test lo verifica
        # directamente contra el ORM, sin pasar por
        # generar_nomina_mensual().
        Nomina.objects.create(fecha_inicio=date(2026, 10, 1), fecha_fin=date(2026, 10, 31))
        with self.assertRaises(IntegrityError):
            Nomina.objects.create(fecha_inicio=date(2026, 10, 1), fecha_fin=date(2026, 10, 31))



# Integrado desde Mónica actual: pruebas de regresión RRHH/Nómina.
class PagoNominaTest(SistemaBaseTest):
    """Fase 3 (Nómina) - Registrar pago de nómina completa."""

    def setUp(self):
        super().setUp()
        Empleado.objects.create(
            nombre='Carlos Mena', puesto='Analista', departamento='Finanzas',
            fecha_ingreso=date(2020, 1, 10), salario=Decimal('40000.00'), estado='Activo',
        )
        Empleado.objects.create(
            nombre='Ana Vargas', puesto='Contadora', departamento='Finanzas',
            fecha_ingreso=date(2021, 3, 1), salario=Decimal('35000.00'), estado='Activo',
        )
        # Rol separado de quien genera nómina: 'ver_nomina' +
        # 'registrar_pago_nomina', SIN 'gestionar_nomina' - así se
        # comprueba de verdad la segregación de funciones (generar
        # nómina y autorizar su pago son permisos independientes).
        self.grupo_pagador = self.crear_rol('RRHH Pagador', 'ver_nomina', 'registrar_pago_nomina')
        self.usuario_pagador = User.objects.create_user(username='rrhh_pagador', password='Pass12345')
        self.usuario_pagador.groups.add(self.grupo_pagador)

        self.nomina = nomina_servicio.generar_nomina_mensual(2026, 9)
        self.total_neto_generado = self.nomina.total_neto
        self.client.force_login(self.usuario_pagador)

    def test_generacion_no_crea_movimiento_financiero(self):
        # Confirma el punto de partida antes de pagar (Test 8): la
        # generación de esta nómina, ya ejecutada en setUp, no debe
        # haber creado ningún MovimientoFinanciero.
        self.assertEqual(MovimientoFinanciero.objects.filter(nomina=self.nomina).count(), 0)

    def test_pago_correcto_marca_pagada_y_crea_un_movimiento(self):
        response = self.client.post(reverse('rrhh_nomina_pagar', args=[self.nomina.pk]))
        self.assertEqual(response.status_code, 302)

        self.nomina.refresh_from_db()
        self.assertEqual(self.nomina.estado, 'Pagada')
        self.assertIsNotNone(self.nomina.fecha_pago)

        movimientos = MovimientoFinanciero.objects.filter(nomina=self.nomina)
        self.assertEqual(movimientos.count(), 1)
        self.assertEqual(movimientos.first().tipo, 'Gasto')

    def test_monto_del_movimiento_es_el_total_neto_de_la_nomina(self):
        self.client.post(reverse('rrhh_nomina_pagar', args=[self.nomina.pk]))
        movimiento = MovimientoFinanciero.objects.get(nomina=self.nomina)
        self.assertEqual(movimiento.monto, self.total_neto_generado)

    def test_un_solo_movimiento_para_toda_la_nomina_con_varios_empleados(self):
        # La nómina de setUp tiene 2 empleados activos (DetalleNomina
        # con 2 filas); el pago debe seguir siendo UN solo movimiento,
        # nunca uno por empleado.
        self.assertEqual(self.nomina.detalles.count(), 2)
        self.client.post(reverse('rrhh_nomina_pagar', args=[self.nomina.pk]))
        self.assertEqual(MovimientoFinanciero.objects.filter(nomina=self.nomina).count(), 1)

    def test_movimiento_queda_relacionado_con_la_nomina(self):
        self.client.post(reverse('rrhh_nomina_pagar', args=[self.nomina.pk]))
        movimiento = MovimientoFinanciero.objects.get(nomina=self.nomina)
        self.assertEqual(movimiento.nomina_id, self.nomina.pk)
        self.nomina.refresh_from_db()
        self.assertEqual(self.nomina.movimiento_financiero, movimiento)

    def test_doble_pago_es_rechazado_y_no_crea_otro_movimiento(self):
        primera = self.client.post(reverse('rrhh_nomina_pagar', args=[self.nomina.pk]))
        self.assertEqual(primera.status_code, 302)

        segunda = self.client.post(reverse('rrhh_nomina_pagar', args=[self.nomina.pk]), follow=True)
        mensajes = [str(m) for m in segunda.context['messages']]
        self.assertTrue(any('ya fue pagada' in m for m in mensajes))

        self.assertEqual(MovimientoFinanciero.objects.filter(nomina=self.nomina).count(), 1)
        self.nomina.refresh_from_db()
        self.assertEqual(self.nomina.estado, 'Pagada')

    def test_atomicidad_ante_fallo_no_deja_datos_parciales(self):
        # Simula un fallo DESPUES de crear el MovimientoFinanciero pero
        # ANTES de que se confirme la transacción (al marcar la Nomina
        # como Pagada). transaction.atomic() debe revertir TODO,
        # incluido el MovimientoFinanciero ya creado dentro del mismo
        # bloque.
        with patch.object(Nomina, 'save', side_effect=RuntimeError('fallo simulado')):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse('rrhh_nomina_pagar', args=[self.nomina.pk]))

        self.assertEqual(MovimientoFinanciero.objects.filter(nomina=self.nomina).count(), 0)
        self.nomina.refresh_from_db()
        self.assertEqual(self.nomina.estado, 'Pendiente de pago')
        self.assertIsNone(self.nomina.fecha_pago)

    def test_usuario_sin_permiso_no_puede_registrar_pago(self):
        usuario_sin_permiso = User.objects.create_user(username='sin_pago', password='Pass12345')
        grupo_solo_ver = self.crear_rol('RRHH Solo Ver Nomina', 'ver_nomina')
        usuario_sin_permiso.groups.add(grupo_solo_ver)
        self.client.force_login(usuario_sin_permiso)

        response = self.client.post(reverse('rrhh_nomina_pagar', args=[self.nomina.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(MovimientoFinanciero.objects.filter(nomina=self.nomina).count(), 0)
        self.nomina.refresh_from_db()
        self.assertEqual(self.nomina.estado, 'Pendiente de pago')



# Integrado desde Mónica actual: pruebas de regresión RRHH/Nómina.
class PagoNominaConcurrenciaTest(SistemaBaseTest):
    """Test 9 (concurrencia): verifica que rrhh_nomina_pagar use
    realmente select_for_update() sobre Nomina - el mismo mecanismo de
    bloqueo de fila que compras_registrar_pago usa contra Compra -
    en vez de una comprobación de estado sin bloqueo.

    NOTA IMPORTANTE sobre el enfoque: se intentó primero una prueba
    con dos hilos y clientes reales concurrentes (igual que se hizo
    para reproducir la condición de carrera de generar_nomina_mensual
    en una fase anterior). SQLite no soporta bloqueo real a nivel de
    fila - select_for_update() es esencialmente un no-op ahí, y dos
    escritores simultáneos sobre la misma tabla producen
    'OperationalError: database table is locked' en vez de serializar
    la segunda solicitud, que es exactamente el comportamiento real en
    PostgreSQL (la base de datos de producción). Por eso esta prueba
    verifica el MECANISMO (que select_for_update() se invoque sobre el
    QuerySet correcto) de forma determinista, y se apoya en
    test_doble_pago_es_rechazado_y_no_crea_otro_movimiento (arriba)
    para la prueba funcional de que el resultado final es correcto.
    """

    def setUp(self):
        super().setUp()
        Empleado.objects.create(
            nombre='Empleado Concurrencia', puesto='Analista', departamento='Finanzas',
            fecha_ingreso=date(2020, 1, 10), salario=Decimal('30000.00'), estado='Activo',
        )
        grupo = self.crear_rol('RRHH Pagador Concurrencia', 'ver_nomina', 'registrar_pago_nomina')
        self.usuario_pagador = User.objects.create_user(username='pagador_conc', password='Pass12345')
        self.usuario_pagador.groups.add(grupo)
        self.nomina = nomina_servicio.generar_nomina_mensual(2026, 9)
        self.client.force_login(self.usuario_pagador)

    def test_registrar_pago_usa_select_for_update_sobre_nomina(self):
        from django.db.models.query import QuerySet

        llamadas = []
        original = QuerySet.select_for_update

        def espia_select_for_update(self_qs, *args, **kwargs):
            if self_qs.model is Nomina:
                llamadas.append(True)
            return original(self_qs, *args, **kwargs)

        with patch.object(QuerySet, 'select_for_update', espia_select_for_update):
            response = self.client.post(reverse('rrhh_nomina_pagar', args=[self.nomina.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(llamadas, 'rrhh_nomina_pagar debe bloquear la fila de Nomina con select_for_update()')
        self.nomina.refresh_from_db()
        self.assertEqual(self.nomina.estado, 'Pagada')
        self.assertEqual(MovimientoFinanciero.objects.filter(nomina=self.nomina).count(), 1)



# Integrado desde Mónica actual: pruebas de regresión RRHH/Nómina.
class PdfNominaTest(SistemaBaseTest):
    """Fase 4 (Nómina) - PDF completo, recibo individual y snapshot histórico."""

    def setUp(self):
        super().setUp()
        self.empleado_1 = Empleado.objects.create(
            nombre='Carlos Mena', puesto='Analista', departamento='Finanzas',
            fecha_ingreso=date(2020, 1, 10), salario=Decimal('30000.00'), estado='Activo',
        )
        Empleado.objects.create(
            nombre='Ana Vargas', puesto='Contadora', departamento='Finanzas',
            fecha_ingreso=date(2021, 3, 1), salario=Decimal('35000.00'), estado='Activo',
        )
        self.nomina = nomina_servicio.generar_nomina_mensual(2026, 9)
        self.detalle_1 = self.nomina.detalles.get(empleado=self.empleado_1)

        # Mismo permiso que la vista en pantalla (ver_nomina) - sin
        # 'gestionar_nomina' ni 'registrar_pago_nomina': ver y
        # descargar PDF son operaciones de lectura, no requieren más.
        grupo = self.crear_rol('RRHH Ver Nomina', 'ver_nomina')
        self.usuario_lector = User.objects.create_user(username='rrhh_lector', password='Pass12345')
        self.usuario_lector.groups.add(grupo)
        self.client.force_login(self.usuario_lector)

    def test_pdf_nomina_completa_responde_correctamente(self):
        response = self.client.get(reverse('rrhh_nomina_pdf', args=[self.nomina.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response.content.startswith(b'%PDF'))

    def test_pdf_nomina_completa_no_genera_movimiento_ni_modifica_nomina(self):
        self.client.get(reverse('rrhh_nomina_pdf', args=[self.nomina.pk]))
        self.assertEqual(MovimientoFinanciero.objects.filter(nomina=self.nomina).count(), 0)
        self.nomina.refresh_from_db()
        self.assertEqual(self.nomina.estado, 'Pendiente de pago')

    def test_recibo_individual_accesible_en_pantalla_y_en_pdf(self):
        respuesta_html = self.client.get(reverse('rrhh_nomina_recibo', args=[self.detalle_1.pk]))
        self.assertEqual(respuesta_html.status_code, 200)

        respuesta_pdf = self.client.get(reverse('rrhh_nomina_recibo_pdf', args=[self.detalle_1.pk]))
        self.assertEqual(respuesta_pdf.status_code, 200)
        self.assertEqual(respuesta_pdf['Content-Type'], 'application/pdf')
        self.assertTrue(respuesta_pdf.content.startswith(b'%PDF'))

    def test_recibo_no_genera_movimiento_ni_modifica_nomina(self):
        self.client.get(reverse('rrhh_nomina_recibo', args=[self.detalle_1.pk]))
        self.client.get(reverse('rrhh_nomina_recibo_pdf', args=[self.detalle_1.pk]))
        self.assertEqual(MovimientoFinanciero.objects.filter(nomina=self.nomina).count(), 0)
        self.nomina.refresh_from_db()
        self.assertEqual(self.nomina.estado, 'Pendiente de pago')

    def test_recibo_usa_snapshot_historico_no_el_salario_actual_del_empleado(self):
        # Prueba histórica obligatoria (Fase 4, punto 11): el recibo de
        # una nómina ya generada no debe reflejar cambios posteriores
        # en Empleado.salario.
        self.assertEqual(self.detalle_1.salario_base, Decimal('30000.00'))

        self.empleado_1.salario = Decimal('40000.00')
        self.empleado_1.save(update_fields=['salario'])

        self.detalle_1.refresh_from_db()
        # El snapshot en DetalleNomina no cambia (ya cubierto también
        # en GeneracionNominaMensualTest, se reconfirma aquí porque es
        # el dato exacto que consume el recibo).
        self.assertEqual(self.detalle_1.salario_base, Decimal('30000.00'))

        respuesta = self.client.get(reverse('rrhh_nomina_recibo', args=[self.detalle_1.pk]))
        contenido = respuesta.content.decode('utf-8')
        self.assertIn('30000.00', contenido)
        self.assertNotIn('40000.00', contenido)

    def test_usuario_sin_ver_nomina_no_accede_a_pdf_ni_recibo(self):
        usuario_sin_permiso = User.objects.create_user(username='sin_ver_nomina', password='Pass12345')
        self.client.force_login(usuario_sin_permiso)

        for url in [
            reverse('rrhh_nomina_pdf', args=[self.nomina.pk]),
            reverse('rrhh_nomina_recibo', args=[self.detalle_1.pk]),
            reverse('rrhh_nomina_recibo_pdf', args=[self.detalle_1.pk]),
        ]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, url)

        self.assertEqual(MovimientoFinanciero.objects.filter(nomina=self.nomina).count(), 0)



class IntegracionRosaCostosPatronalesTest(SistemaBaseTest):
    def test_costos_patronales_usan_configuracion_empresa(self):
        empresa = DatosEmpresa.obtener()
        empresa.porcentaje_riesgo_laboral = Decimal('1.20')
        empresa.save()

        costos = nomina_servicio.calcular_costos_patronales(Decimal('100000.00'), empresa)

        self.assertEqual(costos['infotep'], Decimal('1000.00'))
        self.assertEqual(costos['riesgo_laboral'], Decimal('1200.00'))
        self.assertEqual(costos['total_costo_patronal'], Decimal('2200.00'))

    def test_riesgo_laboral_rechaza_porcentaje_fuera_de_rango(self):
        empresa = DatosEmpresa.obtener()
        empresa.porcentaje_riesgo_laboral = Decimal('1.50')
        with self.assertRaises(ValidationError):
            empresa.full_clean()

    def test_resumen_rrhh_no_descuenta_ausencia_justificada(self):
        empleado = Empleado.objects.create(
            nombre='Empleado Ausencias', puesto='Analista', departamento='RRHH',
            fecha_ingreso=date(2020, 1, 10), salario=Decimal('30000.00'), estado='Activo',
        )
        hoy = timezone.now().date()
        Ausencia.objects.create(empleado=empleado, fecha=hoy, justificada=True, motivo='Médico')
        Ausencia.objects.create(empleado=empleado, fecha=hoy, justificada=False, motivo='Sin justificar')

        from .views import _detalle_rrhh_actual
        detalle = _detalle_rrhh_actual(Empleado.objects.filter(pk=empleado.pk))

        self.assertEqual(detalle[0]['cantidad_ausencias_mes'], 1)


class IntegracionRosaIATest(TestCase):
    def test_modelos_entrenados_de_rosa_estan_incluidos(self):
        from pathlib import Path
        from .ml import prediccion

        self.assertTrue(Path(prediccion.RUTA_MODELO).exists())
        self.assertTrue(Path(prediccion.RUTA_ENCODER).exists())


# Pruebas de regresión recuperadas del proyecto e70859...; no cambian la lógica de producción.
class PedidoClienteCotizacionEstadosTest(SistemaBaseTest):
    """Valida la semántica de PedidoCliente como cotización: estados
    Pendiente/Confirmado/Rechazado/Cancelado/Completado y la condición
    calculada 'vencido'. Complementa a PedidoClienteDespachoTest sin
    repetir sus casos (despacho parcial, doble despacho, etc.)."""

    def setUp(self):
        super().setUp()
        self.pedido = PedidoCliente.objects.create(cliente=self.cliente, estado='Pendiente', creado_por=self.user)
        self.detalle = DetallePedidoCliente.objects.create(
            pedido=self.pedido, producto=self.producto, cantidad=10, precio_unitario='200.00',
        )

    def test_pedido_pendiente_no_afecta_stock_ni_finanzas(self):
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 10)
        self.assertEqual(MovimientoStock.objects.count(), 0)
        self.assertEqual(MovimientoFinanciero.objects.count(), 0)
        self.assertFalse(self.pedido.vencido)

    def test_pedido_confirmado_no_afecta_stock_ni_finanzas(self):
        response = self.client.post(
            reverse('pedidos_clientes_cambiar_estado', args=[self.pedido.pk]),
            {'estado': 'Confirmado'},
        )
        self.assertEqual(response.status_code, 302)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, 'Confirmado')
        self.assertEqual(MovimientoStock.objects.count(), 0)
        self.assertEqual(MovimientoFinanciero.objects.count(), 0)

    def test_pedido_rechazado_no_afecta_stock_ni_finanzas_y_es_estado_final(self):
        response = self.client.post(
            reverse('pedidos_clientes_cambiar_estado', args=[self.pedido.pk]),
            {'estado': 'Rechazado'},
        )
        self.assertEqual(response.status_code, 302)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, 'Rechazado')
        self.assertEqual(MovimientoStock.objects.count(), 0)
        self.assertEqual(MovimientoFinanciero.objects.count(), 0)

        # Estado final: no puede pasar a Confirmado ni a ningún otro estado.
        response = self.client.post(
            reverse('pedidos_clientes_cambiar_estado', args=[self.pedido.pk]),
            {'estado': 'Confirmado'},
        )
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, 'Rechazado')

    def test_pedido_cancelado_no_afecta_stock_ni_finanzas_y_es_estado_final(self):
        response = self.client.post(
            reverse('pedidos_clientes_cambiar_estado', args=[self.pedido.pk]),
            {'estado': 'Cancelado'},
        )
        self.assertEqual(response.status_code, 302)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, 'Cancelado')
        self.assertEqual(MovimientoStock.objects.count(), 0)
        self.assertEqual(MovimientoFinanciero.objects.count(), 0)

        response = self.client.post(
            reverse('pedidos_clientes_cambiar_estado', args=[self.pedido.pk]),
            {'estado': 'Rechazado'},
        )
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, 'Cancelado')

    def test_pedido_vencido_es_calculado_y_no_puede_confirmarse(self):
        self.pedido.fecha_vigencia = timezone.localdate() - timedelta(days=1)
        self.pedido.save(update_fields=['fecha_vigencia'])
        self.assertTrue(self.pedido.vencido)

        response = self.client.post(
            reverse('pedidos_clientes_cambiar_estado', args=[self.pedido.pk]),
            {'estado': 'Confirmado'},
        )
        self.assertEqual(response.status_code, 302)
        self.pedido.refresh_from_db()
        # Sigue Pendiente: la confirmación fue rechazada por vencimiento.
        self.assertEqual(self.pedido.estado, 'Pendiente')
        self.assertEqual(Venta.objects.count(), 0)

    def test_pedido_con_vigencia_futura_no_esta_vencido_y_si_puede_confirmarse(self):
        self.pedido.fecha_vigencia = timezone.localdate() + timedelta(days=5)
        self.pedido.save(update_fields=['fecha_vigencia'])
        self.assertFalse(self.pedido.vencido)

        response = self.client.post(
            reverse('pedidos_clientes_cambiar_estado', args=[self.pedido.pk]),
            {'estado': 'Confirmado'},
        )
        self.assertEqual(response.status_code, 302)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, 'Confirmado')

    def test_pedido_confirmado_ya_no_se_considera_vencido_aunque_pase_la_fecha(self):
        # Una vez aceptado, la vigencia de la oferta deja de aplicar:
        # lo que importa de aquí en adelante es facturarlo, no si la
        # cotización original habría vencido.
        self.pedido.fecha_vigencia = timezone.localdate() - timedelta(days=1)
        self.pedido.estado = 'Confirmado'
        self.pedido.save(update_fields=['fecha_vigencia', 'estado'])
        self.assertFalse(self.pedido.vencido)

    def test_pedido_confirmado_puede_despacharse_y_generar_venta_trazable(self):
        self.pedido.estado = 'Confirmado'
        self.pedido.save(update_fields=['estado'])

        response = self.client.post(
            reverse('pedidos_clientes_despachar', args=[self.pedido.pk]),
            {f'cantidad_{self.detalle.pk}': '10', 'metodo_pago': 'Efectivo'},
        )
        self.assertEqual(response.status_code, 302)

        self.assertEqual(Venta.objects.count(), 1)
        venta = Venta.objects.get()
        self.assertEqual(venta.pedido_id, self.pedido.pk)

        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 0)
        self.assertEqual(MovimientoStock.objects.count(), 1)
        self.assertEqual(MovimientoFinanciero.objects.count(), 1)

        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, 'Completado')

    def test_pedido_creado_con_fecha_vigencia_futura_no_esta_vencido(self):
        pedido = PedidoCliente.objects.create(
            cliente=self.cliente, estado='Pendiente', creado_por=self.user,
            fecha_vigencia=timezone.localdate() + timedelta(days=3),
        )
        self.assertFalse(pedido.vencido)

    def test_pedido_creado_sin_fecha_vigencia_nunca_esta_vencido(self):
        pedido = PedidoCliente.objects.create(
            cliente=self.cliente, estado='Pendiente', creado_por=self.user,
        )
        self.assertIsNone(pedido.fecha_vigencia)
        self.assertFalse(pedido.vencido)
        # Aunque pase mucho tiempo, sigue sin vencer: no hay fecha límite.
        pedido.fecha_creacion = timezone.now() - timedelta(days=365)
        pedido.save(update_fields=['fecha_creacion'])
        self.assertFalse(pedido.vencido)

    def test_no_se_puede_despachar_directamente_un_pedido_vencido(self):
        self.pedido.fecha_vigencia = timezone.localdate() - timedelta(days=1)
        self.pedido.save(update_fields=['fecha_vigencia'])
        # Sigue 'Pendiente' (vencido, pero nunca llegó a Confirmado).
        response = self.client.post(
            reverse('pedidos_clientes_despachar', args=[self.pedido.pk]),
            {f'cantidad_{self.detalle.pk}': '5', 'metodo_pago': 'Efectivo'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Venta.objects.count(), 0)
        self.assertEqual(MovimientoStock.objects.count(), 0)
        self.assertEqual(MovimientoFinanciero.objects.count(), 0)
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 10)

    def test_pedido_rechazado_o_cancelado_no_puede_facturarse(self):
        for estado_final in ('Rechazado', 'Cancelado'):
            with self.subTest(estado=estado_final):
                pedido = PedidoCliente.objects.create(
                    cliente=self.cliente, estado=estado_final, creado_por=self.user,
                )
                detalle = DetallePedidoCliente.objects.create(
                    pedido=pedido, producto=self.producto, cantidad=2, precio_unitario='200.00',
                )
                response = self.client.post(
                    reverse('pedidos_clientes_despachar', args=[pedido.pk]),
                    {f'cantidad_{detalle.pk}': '2', 'metodo_pago': 'Efectivo'},
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(Venta.objects.filter(pedido=pedido).count(), 0)
                self.assertEqual(MovimientoStock.objects.count(), 0)
                self.assertEqual(MovimientoFinanciero.objects.count(), 0)

    def test_estado_invalido_es_rechazado(self):
        response = self.client.post(
            reverse('pedidos_clientes_cambiar_estado', args=[self.pedido.pk]),
            {'estado': 'Vencido'},
        )
        self.assertEqual(response.status_code, 302)
        self.pedido.refresh_from_db()
        # 'Vencido' no es un estado persistible (es calculado): el
        # pedido conserva su estado original.
        self.assertEqual(self.pedido.estado, 'Pendiente')

class ClienteCodigoYFichaTest(SistemaBaseTest):
    """Cliente.codigo se genera automáticamente y es único; la ficha
    expone los campos nuevos (RNC/Cédula, email, dirección)."""

    def test_codigo_se_genera_automaticamente(self):
        cliente = Cliente.objects.create(nombre='Nuevo Cliente')
        self.assertEqual(cliente.codigo, f'CLI-{cliente.pk:06d}')

    def test_codigo_es_unico_entre_clientes(self):
        c1 = Cliente.objects.create(nombre='Cliente A')
        c2 = Cliente.objects.create(nombre='Cliente B')
        self.assertNotEqual(c1.codigo, c2.codigo)

    def test_codigo_explicito_se_respeta_si_se_provee(self):
        cliente = Cliente.objects.create(nombre='Cliente Manual', codigo='CLI-999999')
        self.assertEqual(cliente.codigo, 'CLI-999999')

    def test_ficha_expone_todos_los_pedidos_no_solo_pendientes(self):
        pedido_completado = PedidoCliente.objects.create(cliente=self.cliente, estado='Completado', creado_por=self.user)
        pedido_rechazado = PedidoCliente.objects.create(cliente=self.cliente, estado='Rechazado', creado_por=self.user)

        response = self.client.get(reverse('clientes_detalle', args=[self.cliente.pk]))
        pedidos_en_contexto = set(response.context['pedidos'])
        self.assertIn(pedido_completado, pedidos_en_contexto)
        self.assertIn(pedido_rechazado, pedidos_en_contexto)

class ClienteCrearAjaxTest(SistemaBaseTest):
    """Creación rápida de cliente desde '+ Nuevo cliente' en Nueva Venta."""

    def test_crear_cliente_via_ajax_devuelve_codigo_y_id(self):
        response = self.client.post(
            reverse('clientes_crear_ajax'),
            data=json.dumps({'nombre': 'Cliente Rápido', 'rnc_cedula': '001-1234567-8'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(Cliente.objects.filter(pk=data['id'], nombre='Cliente Rápido').exists())
        self.assertTrue(data['codigo'].startswith('CLI-'))

    def test_crear_cliente_via_ajax_sin_nombre_falla(self):
        response = self.client.post(
            reverse('clientes_crear_ajax'),
            data=json.dumps({'nombre': ''}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.json())

    def test_crear_cliente_via_ajax_requiere_metodo_post(self):
        response = self.client.get(reverse('clientes_crear_ajax'))
        self.assertEqual(response.status_code, 405)

class VentaDescuentoFacturaYSnapshotTest(SistemaBaseTest):
    """Numeración de factura, descuento por línea calculado en el
    backend, condición de pago, y snapshot histórico del cliente."""

    def test_numero_factura_se_genera_automaticamente_y_es_secuencial(self):
        self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({'productos': [{'sku': self.producto.sku, 'cantidad': 1}]}),
            content_type='application/json',
        )
        self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({'productos': [{'sku': self.producto.sku, 'cantidad': 1}]}),
            content_type='application/json',
        )
        ventas = list(Venta.objects.order_by('id'))
        self.assertEqual(len(ventas), 2)
        self.assertEqual(ventas[0].numero_factura, f'FAC-{ventas[0].pk:06d}')
        self.assertEqual(ventas[1].numero_factura, f'FAC-{ventas[1].pk:06d}')
        self.assertNotEqual(ventas[0].numero_factura, ventas[1].numero_factura)

    def test_descuento_por_linea_se_calcula_en_el_backend(self):
        # Producto a 200.00, cantidad 2 = 400.00 bruto. 10% descuento = 40.00.
        response = self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({
                'productos': [{'sku': self.producto.sku, 'cantidad': 2, 'descuento_porcentaje': 10}],
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        venta = Venta.objects.get()
        self.assertEqual(venta.subtotal_bruto, Decimal('400.00'))
        self.assertEqual(venta.descuento, Decimal('40.00'))
        self.assertEqual(venta.subtotal, Decimal('360.00'))

    def test_descuento_fuera_de_rango_es_rechazado(self):
        response = self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({
                'productos': [{'sku': self.producto.sku, 'cantidad': 1, 'descuento_porcentaje': 150}],
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Venta.objects.count(), 0)

    def test_venta_al_contado_genera_movimiento_financiero(self):
        self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({
                'productos': [{'sku': self.producto.sku, 'cantidad': 1}],
                'condicion_pago': 'Contado',
            }),
            content_type='application/json',
        )
        self.assertEqual(MovimientoFinanciero.objects.count(), 1)

    def test_venta_a_credito_no_genera_movimiento_financiero_de_inmediato(self):
        # Fase 6: una venta a crédito ahora genera una CuentaPorCobrar,
        # no un MovimientoFinanciero directo — el ingreso real ocurre
        # cuando se registra un Cobro, no al momento de vender. Y una
        # venta a crédito requiere un cliente real (no "Cliente
        # Genérico"), porque la CxC necesita a quién atribuir la deuda.
        response = self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({
                'productos': [{'sku': self.producto.sku, 'cantidad': 1}],
                'cliente_id': self.cliente.id,
                'condicion_pago': 'Credito',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        venta = Venta.objects.get()
        self.assertEqual(venta.condicion_pago, 'Credito')
        self.assertEqual(MovimientoFinanciero.objects.count(), 0)
        self.assertEqual(CuentaPorCobrar.objects.filter(venta=venta).count(), 1)

    def test_snapshot_historico_no_cambia_si_el_cliente_se_edita_despues(self):
        cliente = Cliente.objects.create(nombre='Cliente Original', rnc_cedula='001-0000000-0')
        self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({
                'productos': [{'sku': self.producto.sku, 'cantidad': 1}],
                'cliente_id': cliente.pk,
            }),
            content_type='application/json',
        )
        venta = Venta.objects.get()
        self.assertEqual(venta.cliente_nombre, 'Cliente Original')

        cliente.nombre = 'Cliente Editado'
        cliente.rnc_cedula = '999-9999999-9'
        cliente.save()

        venta.refresh_from_db()
        self.assertEqual(venta.cliente_nombre, 'Cliente Original')
        self.assertEqual(venta.cliente_rnc_cedula, '001-0000000-0')

    def test_venta_factura_preview_accesible(self):
        self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({'productos': [{'sku': self.producto.sku, 'cantidad': 1}]}),
            content_type='application/json',
        )
        venta = Venta.objects.get()
        response = self.client.get(reverse('venta_factura', args=[venta.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, venta.numero_factura)

    def test_venta_factura_pdf_descarga(self):
        self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({'productos': [{'sku': self.producto.sku, 'cantidad': 1}]}),
            content_type='application/json',
        )
        venta = Venta.objects.get()
        response = self.client.get(reverse('venta_factura_pdf', args=[venta.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')

class PedidoEstadoProcesamientoEnVentasTest(SistemaBaseTest):
    """El tab 'Pedidos' embebido en Ventas muestra estado_procesamiento
    (Pendiente/Entrega parcial/Facturado), no el estado interno crudo."""

    def test_ventas_expone_pedidos_con_estado_procesamiento(self):
        pedido = PedidoCliente.objects.create(cliente=self.cliente, estado='Confirmado', creado_por=self.user)
        detalle = DetallePedidoCliente.objects.create(
            pedido=pedido, producto=self.producto, cantidad=10, precio_unitario='200.00',
        )

        response = self.client.get(reverse('ventas'))
        self.assertEqual(response.status_code, 200)
        pedidos_en_contexto = list(response.context['pedidos_lista'])
        self.assertIn(pedido, pedidos_en_contexto)
        self.assertEqual(pedido.estado_procesamiento, 'Pendiente')

        self.client.post(
            reverse('pedidos_clientes_despachar', args=[pedido.pk]),
            {f'cantidad_{detalle.pk}': '4', 'metodo_pago': 'Efectivo'},
        )
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado_procesamiento, 'Entrega parcial')

        self.client.post(
            reverse('pedidos_clientes_despachar', args=[pedido.pk]),
            {f'cantidad_{detalle.pk}': '6', 'metodo_pago': 'Efectivo'},
        )
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado_procesamiento, 'Facturado')

    def test_pedidos_cancelados_no_aparecen_en_el_tab_de_ventas(self):
        pedido_cancelado = PedidoCliente.objects.create(cliente=self.cliente, estado='Cancelado', creado_por=self.user)
        response = self.client.get(reverse('ventas'))
        self.assertNotIn(pedido_cancelado, list(response.context['pedidos_lista']))

class FacturaDisenoProfesionalTest(SistemaBaseTest):
    """Segunda etapa: presentación de la factura (preview + PDF).
    La fuente de verdad sigue siendo Venta/DetalleVenta; estos tests
    verifican que el nuevo diseño no alteró esos datos ni la lógica
    financiera ya validada en VentaDescuentoFacturaYSnapshotTest."""

    def _crear_venta(self, cliente=None, descuento_pct=0, cantidad=1, con_pedido=False):
        producto = self.producto
        if con_pedido:
            pedido = PedidoCliente.objects.create(cliente=cliente or self.cliente, estado='Confirmado', creado_por=self.user)
            detalle = DetallePedidoCliente.objects.create(
                pedido=pedido, producto=producto, cantidad=cantidad, precio_unitario='200.00',
            )
            self.client.post(
                reverse('pedidos_clientes_despachar', args=[pedido.pk]),
                {f'cantidad_{detalle.pk}': str(cantidad), 'metodo_pago': 'Efectivo'},
            )
            return Venta.objects.get()
        payload = {'productos': [{'sku': producto.sku, 'cantidad': cantidad, 'descuento_porcentaje': descuento_pct}]}
        if cliente:
            payload['cliente_id'] = cliente.pk
        self.client.post(reverse('procesar_venta'), data=json.dumps(payload), content_type='application/json')
        return Venta.objects.latest('id')

    def test_vista_previa_responde_correctamente(self):
        venta = self._crear_venta()
        response = self.client.get(reverse('venta_factura', args=[venta.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, venta.numero_factura)
        self.assertContains(response, 'no constituye una integración certificada de e-CF con DGII')

    def test_vista_previa_usa_snapshot_historico_no_datos_actuales_del_cliente(self):
        cliente = Cliente.objects.create(nombre='Cliente Snapshot', rnc_cedula='001-1111111-1')
        venta = self._crear_venta(cliente=cliente)
        cliente.nombre = 'Nombre Cambiado Después'
        cliente.rnc_cedula = '002-2222222-2'
        cliente.save()

        response = self.client.get(reverse('venta_factura', args=[venta.pk]))
        self.assertContains(response, 'Cliente Snapshot')
        self.assertContains(response, '001-1111111-1')
        self.assertNotContains(response, 'Nombre Cambiado Después')

    def test_descuento_aparece_correctamente_en_la_vista_previa(self):
        venta = self._crear_venta(descuento_pct=10, cantidad=2)
        response = self.client.get(reverse('venta_factura', args=[venta.pk]))
        self.assertContains(response, f'RD$ {venta.descuento}')

    def test_subtotal_bruto_correcto(self):
        venta = self._crear_venta(cantidad=3)
        # Producto a 200.00 x 3 = 600.00
        self.assertEqual(venta.subtotal_bruto, Decimal('600.00'))

    def test_subtotal_neto_correcto_con_descuento(self):
        venta = self._crear_venta(descuento_pct=25, cantidad=2)
        # 400.00 bruto - 25% (100.00) = 300.00 neto
        self.assertEqual(venta.subtotal, Decimal('300.00'))

    def test_itbis_usa_el_porcentaje_configurado_no_18_hardcodeado(self):
        DatosEmpresa.objects.update_or_create(pk=1, defaults={'itbs_porcentaje': '16.00'})
        venta = self._crear_venta(cantidad=1)
        # 200.00 neto x 16% = 32.00 (no 36.00, que sería con 18%)
        self.assertEqual(venta.itbs, Decimal('32.00'))

    def test_total_correcto(self):
        venta = self._crear_venta(cantidad=1)
        self.assertEqual(venta.total, venta.subtotal + venta.itbs)

    def test_itbs_linea_se_prorratea_desde_el_itbis_historico_de_la_venta(self):
        venta = self._crear_venta(cantidad=2)
        detalle = venta.detalles.get()
        # Una sola línea: el ITBIS de la línea es el ITBIS total de la venta.
        self.assertEqual(detalle.itbs_linea, venta.itbs)

    def test_descarga_pdf_funciona(self):
        venta = self._crear_venta()
        response = self.client.get(reverse('venta_factura_pdf', args=[venta.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertGreater(len(response.content), 0)

    def test_pdf_funciona_sin_pedido_relacionado(self):
        venta = self._crear_venta()
        self.assertIsNone(venta.pedido)
        response = self.client.get(reverse('venta_factura_pdf', args=[venta.pk]))
        self.assertEqual(response.status_code, 200)

    def test_pdf_funciona_con_pedido_relacionado(self):
        venta = self._crear_venta(con_pedido=True, cantidad=2)
        self.assertIsNotNone(venta.pedido)
        response = self.client.get(reverse('venta_factura_pdf', args=[venta.pk]))
        self.assertEqual(response.status_code, 200)

    def test_pdf_funciona_con_campos_opcionales_vacios(self):
        # Cliente genérico (sin cliente_id): cliente_rnc_cedula, telefono,
        # direccion, email quedan vacíos; venta.observaciones también.
        venta = self._crear_venta()
        self.assertEqual(venta.cliente_rnc_cedula, '')
        self.assertEqual(venta.observaciones, '')
        response = self.client.get(reverse('venta_factura_pdf', args=[venta.pk]))
        self.assertEqual(response.status_code, 200)

    def test_pdf_funciona_con_multiples_lineas(self):
        producto2 = Producto.objects.create(
            sku='T002', nombre='Segundo Producto', categoria='Laptops',
            costo_compra='50.00', precio_venta='75.00', stock=10, stock_minimo=2,
        )
        response = self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({'productos': [
                {'sku': self.producto.sku, 'cantidad': 2, 'descuento_porcentaje': 10},
                {'sku': producto2.sku, 'cantidad': 3},
            ]}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        venta = Venta.objects.latest('id')
        self.assertEqual(venta.detalles.count(), 2)
        response = self.client.get(reverse('venta_factura_pdf', args=[venta.pk]))
        self.assertEqual(response.status_code, 200)

    def test_factura_historica_no_cambia_al_modificar_cliente_despues(self):
        cliente = Cliente.objects.create(nombre='Cliente Histórico', direccion='Calle Original')
        venta = self._crear_venta(cliente=cliente)

        cliente.direccion = 'Calle Nueva'
        cliente.save()

        venta.refresh_from_db()
        self.assertEqual(venta.cliente_direccion, 'Calle Original')
        response = self.client.get(reverse('venta_factura', args=[venta.pk]))
        self.assertContains(response, 'Calle Original')
        self.assertNotContains(response, 'Calle Nueva')

class CuentaPorCobrarCreacionTest(SistemaBaseTest):
    """Fase 6: 1 Venta a crédito -> 1 CuentaPorCobrar, nunca al contado."""

    def _vender(self, condicion_pago='Contado', cliente_id=None, sku=None, cantidad=1):
        return self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({
                'productos': [{'sku': sku or self.producto.sku, 'cantidad': cantidad}],
                'cliente_id': cliente_id if cliente_id is not None else self.cliente.id,
                'condicion_pago': condicion_pago,
            }),
            content_type='application/json',
        )

    def test_venta_contado_no_crea_cxc(self):
        response = self._vender('Contado')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CuentaPorCobrar.objects.count(), 0)

    def test_venta_credito_crea_exactamente_una_cxc(self):
        response = self._vender('Credito')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CuentaPorCobrar.objects.count(), 1)
        venta = Venta.objects.get()
        cuenta = CuentaPorCobrar.objects.get()
        self.assertEqual(cuenta.venta_id, venta.pk)
        self.assertEqual(cuenta.cliente_id, self.cliente.id)

    def test_venta_credito_sin_cliente_es_rechazada(self):
        response = self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({
                'productos': [{'sku': self.producto.sku, 'cantidad': 1}],
                'condicion_pago': 'Credito',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Venta.objects.count(), 0)
        self.assertEqual(CuentaPorCobrar.objects.count(), 0)

    def test_importe_original_igual_al_total_de_la_venta(self):
        self._vender('Credito')
        venta = Venta.objects.get()
        cuenta = CuentaPorCobrar.objects.get()
        self.assertEqual(cuenta.importe_original, venta.total)

    def test_fecha_vencimiento_usa_dias_credito_de_datosempresa(self):
        empresa = DatosEmpresa.obtener()
        empresa.dias_credito = 45
        empresa.save(update_fields=['dias_credito'])

        self._vender('Credito')
        venta = Venta.objects.get()
        cuenta = CuentaPorCobrar.objects.get()
        self.assertEqual(cuenta.fecha_vencimiento, venta.fecha.date() + timedelta(days=45))

    def test_cambiar_dias_credito_no_altera_cxc_ya_emitidas(self):
        self._vender('Credito')
        cuenta = CuentaPorCobrar.objects.get()
        vencimiento_original = cuenta.fecha_vencimiento

        empresa = DatosEmpresa.obtener()
        empresa.dias_credito = 90
        empresa.save(update_fields=['dias_credito'])

        cuenta.refresh_from_db()
        self.assertEqual(cuenta.fecha_vencimiento, vencimiento_original)

    def test_venta_a_credito_no_permite_una_segunda_cxc_para_la_misma_venta(self):
        # La regla de negocio "1 Venta -> 1 CxC" la garantiza el propio
        # OneToOneField a nivel de base de datos, no una validación en
        # la vista: crear una segunda CuentaPorCobrar para la misma
        # Venta debe fallar como violación de integridad.
        from django.db import IntegrityError
        self._vender('Credito')
        venta = Venta.objects.get()
        with self.assertRaises(IntegrityError):
            CuentaPorCobrar.objects.create(
                venta=venta, cliente=self.cliente, importe_original=venta.total,
                fecha_emision=venta.fecha.date(),
                fecha_vencimiento=venta.fecha.date() + timedelta(days=30),
            )

class CuentaPorCobrarCobrosTest(SistemaBaseTest):
    """Fase 6: pagos parciales/completos, historial, y rechazo explícito
    de sobrepago (nunca min(monto, saldo))."""

    def setUp(self):
        super().setUp()
        self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({
                'productos': [{'sku': self.producto.sku, 'cantidad': 5}],
                'cliente_id': self.cliente.id,
                'condicion_pago': 'Credito',
            }),
            content_type='application/json',
        )
        self.cuenta = CuentaPorCobrar.objects.get()
        # 5 unidades x RD$200 x 1.18 ITBIS = RD$1180.00
        self.assertEqual(self.cuenta.importe_original, Decimal('1180.00'))

    def _cobrar(self, monto, metodo_pago='Efectivo'):
        return self.client.post(
            reverse('cuenta_por_cobrar_registrar_cobro', args=[self.cuenta.pk]),
            {'monto': str(monto), 'fecha': timezone.now().date(), 'metodo_pago': metodo_pago},
        )

    def test_cobro_parcial_reduce_el_saldo(self):
        response = self._cobrar('300.00')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.cuenta.saldo_pendiente, Decimal('880.00'))
        self.assertEqual(self.cuenta.estado, CuentaPorCobrar.ESTADO_PENDIENTE)

    def test_multiples_cobros_parciales_se_suman_correctamente(self):
        self._cobrar('300.00')
        self._cobrar('200.00')
        self._cobrar('180.00')
        self.assertEqual(self.cuenta.total_cobrado, Decimal('680.00'))
        self.assertEqual(self.cuenta.saldo_pendiente, Decimal('500.00'))
        self.assertEqual(Cobro.objects.filter(cuenta=self.cuenta).count(), 3)

    def test_cobro_que_completa_el_saldo_pasa_a_pagada(self):
        self._cobrar('1180.00')
        self.assertEqual(self.cuenta.saldo_pendiente, Decimal('0.00'))
        self.assertEqual(self.cuenta.estado, CuentaPorCobrar.ESTADO_PAGADA)

    def test_sobrepago_es_rechazado_sin_crear_nada(self):
        response = self._cobrar('2000.00')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Cobro.objects.count(), 0)
        self.assertEqual(MovimientoFinanciero.objects.count(), 0)
        self.assertEqual(self.cuenta.saldo_pendiente, Decimal('1180.00'))

    def test_no_se_puede_cobrar_una_cuenta_ya_pagada(self):
        self._cobrar('1180.00')
        response = self._cobrar('50.00')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Cobro.objects.filter(cuenta=self.cuenta).count(), 1)

    def test_cxc_vencida_sigue_permitiendo_cobros(self):
        self.cuenta.fecha_vencimiento = timezone.now().date() - timedelta(days=5)
        self.cuenta.save(update_fields=['fecha_vencimiento'])
        self.assertEqual(self.cuenta.estado, CuentaPorCobrar.ESTADO_VENCIDA)

        response = self._cobrar('300.00')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.cuenta.saldo_pendiente, Decimal('880.00'))

    def test_historial_de_cobro_conserva_fecha_monto_metodo_y_usuario(self):
        self._cobrar('300.00', metodo_pago='Transferencia')
        cobro = Cobro.objects.get()
        self.assertEqual(cobro.monto, Decimal('300.00'))
        self.assertEqual(cobro.metodo_pago, 'Transferencia')
        self.assertEqual(cobro.registrado_por, self.user)

    def test_select_for_update_serializa_dos_cobros_secuenciales(self):
        # No se simulan hilos reales (el proyecto no tiene ese patrón
        # de pruebas hasta ahora): esta prueba confirma la garantía que
        # select_for_update() hace posible bajo concurrencia real -
        # que la segunda escritura se valida contra el saldo YA
        # actualizado por la primera, nunca contra un saldo leído antes
        # de que la primera se confirmara. Si el saldo se validara solo
        # en el formulario (contra un valor mostrado previamente), un
        # segundo cobro de RD$900 tras uno de RD$300 sobre un saldo de
        # RD$1180 habría parecido válido (300+900=1200 > 1180 realmente,
        # pero cada uno por separado es < 1180).
        primera = self._cobrar('300.00')
        segunda = self._cobrar('900.00')
        self.assertEqual(primera.status_code, 302)
        self.assertEqual(segunda.status_code, 302)
        self.assertEqual(Cobro.objects.filter(cuenta=self.cuenta).count(), 1)
        self.assertEqual(self.cuenta.saldo_pendiente, Decimal('880.00'))

class CuentaPorCobrarFinanzasTest(SistemaBaseTest):
    """Fase 6: la venta a crédito NO genera ingreso; el cobro sí, y cada
    cobro genera exactamente un MovimientoFinanciero (OneToOne)."""

    def test_venta_credito_no_genera_movimiento_financiero(self):
        self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({
                'productos': [{'sku': self.producto.sku, 'cantidad': 1}],
                'cliente_id': self.cliente.id,
                'condicion_pago': 'Credito',
            }),
            content_type='application/json',
        )
        self.assertEqual(MovimientoFinanciero.objects.count(), 0)

    def test_cada_cobro_genera_exactamente_un_movimiento_financiero_de_ingreso(self):
        self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({
                'productos': [{'sku': self.producto.sku, 'cantidad': 2}],
                'cliente_id': self.cliente.id,
                'condicion_pago': 'Credito',
            }),
            content_type='application/json',
        )
        cuenta = CuentaPorCobrar.objects.get()
        self.client.post(
            reverse('cuenta_por_cobrar_registrar_cobro', args=[cuenta.pk]),
            {'monto': '100.00', 'fecha': timezone.now().date(), 'metodo_pago': 'Efectivo'},
        )
        cobro = Cobro.objects.get()
        movimiento = MovimientoFinanciero.objects.get()
        self.assertEqual(movimiento.tipo, 'Ingreso')
        self.assertEqual(movimiento.monto, Decimal('100.00'))
        self.assertEqual(movimiento.cobro_id, cobro.pk)
        self.assertIsNone(movimiento.venta_id)

class CuentaPorCobrarSeguridadTest(SistemaBaseTest):
    def setUp(self):
        super().setUp()
        self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({
                'productos': [{'sku': self.producto.sku, 'cantidad': 1}],
                'cliente_id': self.cliente.id,
                'condicion_pago': 'Credito',
            }),
            content_type='application/json',
        )
        self.cuenta = CuentaPorCobrar.objects.get()

    def test_usuario_sin_permiso_no_puede_ver_detalle_de_cxc(self):
        usuario = User.objects.create_user(username='sin_permiso', password='Pass12345')
        self.client.force_login(usuario)
        response = self.client.get(reverse('cuenta_por_cobrar_detalle', args=[self.cuenta.pk]))
        self.assertEqual(response.status_code, 403)

    def test_usuario_sin_permiso_no_puede_registrar_cobro(self):
        usuario = User.objects.create_user(username='sin_permiso', password='Pass12345')
        self.client.force_login(usuario)
        response = self.client.post(
            reverse('cuenta_por_cobrar_registrar_cobro', args=[self.cuenta.pk]),
            {'monto': '50.00', 'fecha': timezone.now().date(), 'metodo_pago': 'Efectivo'},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Cobro.objects.count(), 0)

    def test_usuario_con_ver_pero_no_registrar_no_puede_cobrar(self):
        grupo = self.crear_rol('Solo Consulta CxC', 'ver_finanzas', 'ver_cuentas_por_cobrar')
        usuario = User.objects.create_user(username='consulta', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        puede_ver = self.client.get(reverse('cuenta_por_cobrar_detalle', args=[self.cuenta.pk]))
        no_puede_cobrar = self.client.post(
            reverse('cuenta_por_cobrar_registrar_cobro', args=[self.cuenta.pk]),
            {'monto': '50.00', 'fecha': timezone.now().date(), 'metodo_pago': 'Efectivo'},
        )
        self.assertEqual(puede_ver.status_code, 200)
        self.assertEqual(no_puede_cobrar.status_code, 403)
        self.assertEqual(Cobro.objects.count(), 0)

    def test_usuario_autorizado_si_puede_ver_y_cobrar(self):
        grupo = self.crear_rol(
            'Cobrador', 'ver_finanzas', 'ver_cuentas_por_cobrar', 'registrar_cobros',
        )
        usuario = User.objects.create_user(username='cobrador', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        puede_ver = self.client.get(reverse('cuenta_por_cobrar_detalle', args=[self.cuenta.pk]))
        puede_cobrar = self.client.post(
            reverse('cuenta_por_cobrar_registrar_cobro', args=[self.cuenta.pk]),
            {'monto': '50.00', 'fecha': timezone.now().date(), 'metodo_pago': 'Efectivo'},
        )
        self.assertEqual(puede_ver.status_code, 200)
        self.assertEqual(puede_cobrar.status_code, 302)
        self.assertEqual(Cobro.objects.count(), 1)

    def test_pestana_cxc_no_aparece_sin_permiso_ver_cuentas_por_cobrar(self):
        grupo = self.crear_rol('Solo Finanzas General', 'ver_finanzas')
        usuario = User.objects.create_user(username='finanzas_general', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        response = self.client.get(reverse('finanzas'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'data-tab="cxc"')

class CuentaPorPagarCreacionTest(SistemaBaseTest):
    """Compra contado vs crédito: MovimientoFinanciero.compra se
    conserva para el contado (RN-CXP-02) y el crédito no genera
    egreso al crearse (RN-CXP-03)."""

    def setUp(self):
        super().setUp()
        self.proveedor = Proveedor.objects.create(nombre='Proveedor CxP')

    def _crear_compra(self, condicion_pago='Credito', total='1000.00', fecha_vencimiento=None, **extra):
        data = {
            'proveedor': self.proveedor.pk,
            'numero_factura': 'F-001',
            'fecha': timezone.now().date(),
            'subtotal': total,
            'impuestos': '0',
            'total': total,
            'condicion_pago': condicion_pago,
        }
        if condicion_pago == 'Credito':
            data['fecha_vencimiento'] = fecha_vencimiento or (timezone.now().date() + timedelta(days=30))
        data.update(extra)
        return self.client.post(reverse('compras_lista'), data)

    def test_compra_contado_genera_movimiento_financiero_compra_y_ningun_pagocompra(self):
        response = self._crear_compra('Contado')
        self.assertEqual(response.status_code, 302)
        compra = Compra.objects.get()
        self.assertEqual(compra.estado_pago, 'Pagada')
        self.assertEqual(compra.monto_pagado, compra.total)
        # RN-CXP-02: sigue usando MovimientoFinanciero.compra (FK), sin
        # crear PagoCompra.
        movimiento = MovimientoFinanciero.objects.get()
        self.assertEqual(movimiento.compra_id, compra.pk)
        self.assertIsNone(movimiento.pago_compra_id)
        self.assertEqual(PagoCompra.objects.count(), 0)

    def test_compra_credito_no_genera_movimiento_financiero_al_crearse(self):
        response = self._crear_compra('Credito')
        self.assertEqual(response.status_code, 302)
        compra = Compra.objects.get()
        self.assertEqual(compra.estado_pago, 'Pendiente')
        self.assertEqual(compra.monto_pagado, Decimal('0'))
        # RN-CXP-03: ningún egreso hasta que se registre un pago.
        self.assertEqual(MovimientoFinanciero.objects.count(), 0)
        self.assertEqual(PagoCompra.objects.count(), 0)

    def test_compra_credito_exige_fecha_vencimiento(self):
        response = self.client.post(reverse('compras_lista'), {
            'proveedor': self.proveedor.pk,
            'numero_factura': 'F-002',
            'fecha': timezone.now().date(),
            'subtotal': '500.00',
            'impuestos': '0',
            'total': '500.00',
            'condicion_pago': 'Credito',
            # fecha_vencimiento deliberadamente omitida.
        })
        # El formulario es inválido: se re-renderiza (200), no redirige.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Compra.objects.count(), 0)

    def test_fecha_vencimiento_no_depende_de_dias_credito_de_datosempresa(self):
        # Decisión de diseño explícita: a diferencia de CxC,
        # fecha_vencimiento en Compra NO se deriva de
        # DatosEmpresa.dias_credito (esa es una política hacia
        # clientes, no hacia proveedores). Cambiar dias_credito no debe
        # afectar en absoluto el vencimiento de una Compra.
        empresa = DatosEmpresa.obtener()
        empresa.dias_credito = 999
        empresa.save(update_fields=['dias_credito'])

        vencimiento_elegido = timezone.now().date() + timedelta(days=10)
        self._crear_compra('Credito', fecha_vencimiento=vencimiento_elegido)
        compra = Compra.objects.get()
        self.assertEqual(compra.fecha_vencimiento, vencimiento_elegido)

class CuentaPorPagarPagosTest(SistemaBaseTest):
    """Pagos parciales/completos, historial, rechazo de sobrepago y de
    monto <= 0 (RN-CXP-04 a RN-CXP-10)."""

    def setUp(self):
        super().setUp()
        self.proveedor = Proveedor.objects.create(nombre='Proveedor CxP')
        self.compra = Compra.objects.create(
            proveedor=self.proveedor, numero_factura='F-100',
            subtotal=Decimal('1000.00'), total=Decimal('1000.00'),
            condicion_pago='Credito', estado_pago='Pendiente',
            fecha_vencimiento=timezone.now().date() + timedelta(days=30),
            creado_por=self.user,
        )

    def _pagar(self, monto, metodo_pago='Efectivo'):
        return self.client.post(
            reverse('compras_registrar_pago', args=[self.compra.pk]),
            {'monto': str(monto), 'fecha': timezone.now().date(), 'metodo_pago': metodo_pago},
        )

    def test_pago_parcial_reduce_el_saldo(self):
        response = self._pagar('300.00')
        self.assertEqual(response.status_code, 302)
        self.compra.refresh_from_db()
        self.assertEqual(self.compra.saldo_pendiente, Decimal('700.00'))
        self.assertEqual(self.compra.estado_pago, 'Parcial')

    def test_multiples_pagos_parciales_se_suman_correctamente(self):
        self._pagar('300.00')
        self._pagar('200.00')
        self._pagar('180.00')
        self.compra.refresh_from_db()
        self.assertEqual(self.compra.monto_pagado, Decimal('680.00'))
        self.assertEqual(self.compra.saldo_pendiente, Decimal('320.00'))
        self.assertEqual(PagoCompra.objects.filter(compra=self.compra).count(), 3)

    def test_pago_que_completa_el_saldo_pasa_a_pagada(self):
        self._pagar('1000.00')
        self.compra.refresh_from_db()
        self.assertEqual(self.compra.saldo_pendiente, Decimal('0.00'))
        self.assertEqual(self.compra.estado_pago, 'Pagada')
        self.assertEqual(self.compra.estado_cxp, 'Pagada')

    def test_sobrepago_es_rechazado_sin_crear_nada(self):
        response = self._pagar('5000.00')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PagoCompra.objects.count(), 0)
        self.assertEqual(MovimientoFinanciero.objects.count(), 0)
        self.compra.refresh_from_db()
        self.assertEqual(self.compra.saldo_pendiente, Decimal('1000.00'))
        self.assertEqual(self.compra.monto_pagado, Decimal('0'))

    def test_monto_cero_o_negativo_es_rechazado(self):
        for monto in ('0', '-50.00'):
            response = self._pagar(monto)
            self.assertEqual(response.status_code, 302)
        self.assertEqual(PagoCompra.objects.count(), 0)
        self.compra.refresh_from_db()
        self.assertEqual(self.compra.monto_pagado, Decimal('0'))

    def test_no_se_puede_pagar_una_compra_ya_pagada(self):
        self._pagar('1000.00')
        response = self._pagar('50.00')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PagoCompra.objects.filter(compra=self.compra).count(), 1)

    def test_compra_vencida_sigue_permitiendo_pagos(self):
        self.compra.fecha_vencimiento = timezone.now().date() - timedelta(days=5)
        self.compra.save(update_fields=['fecha_vencimiento'])
        self.assertTrue(self.compra.vencida)
        self.assertEqual(self.compra.estado_cxp, 'Vencida')

        response = self._pagar('300.00')
        self.assertEqual(response.status_code, 302)
        self.compra.refresh_from_db()
        self.assertEqual(self.compra.saldo_pendiente, Decimal('700.00'))

    def test_compra_sin_vencer_no_esta_vencida(self):
        self.assertFalse(self.compra.vencida)
        self.assertEqual(self.compra.estado_cxp, 'Pendiente')

    def test_historial_de_pago_conserva_fecha_monto_metodo_y_usuario(self):
        self._pagar('300.00', metodo_pago='Transferencia')
        pago = PagoCompra.objects.get()
        self.assertEqual(pago.monto, Decimal('300.00'))
        self.assertEqual(pago.metodo_pago, 'Transferencia')
        self.assertEqual(pago.registrado_por, self.user)

    def test_cada_pago_genera_exactamente_un_movimiento_financiero_de_gasto(self):
        self._pagar('100.00')
        pago = PagoCompra.objects.get()
        movimiento = MovimientoFinanciero.objects.get()
        self.assertEqual(movimiento.tipo, 'Gasto')
        self.assertEqual(movimiento.monto, Decimal('100.00'))
        self.assertEqual(movimiento.pago_compra_id, pago.pk)
        # RN-CXP-13: no se toca la relación 'compra' (esa es exclusiva
        # del flujo de contado).
        self.assertIsNone(movimiento.compra_id)

    def test_movimiento_financiero_pago_compra_es_relacion_one_to_one(self):
        # Un PagoCompra no puede terminar asociado a dos
        # MovimientoFinanciero: lo garantiza el propio OneToOneField a
        # nivel de base de datos.
        from django.db import IntegrityError
        self._pagar('100.00')
        pago = PagoCompra.objects.get()
        with self.assertRaises(IntegrityError):
            MovimientoFinanciero.objects.create(
                tipo='Gasto', fecha=timezone.now().date(), categoria='Compras',
                monto=Decimal('1.00'), pago_compra=pago,
            )

    def test_select_for_update_serializa_dos_pagos_secuenciales(self):
        # Mismo criterio que CuentaPorCobrarCobrosTest
        # .test_select_for_update_serializa_dos_cobros_secuenciales: no
        # se simulan hilos reales, pero esta prueba confirma que el
        # saldo se valida contra el estado YA actualizado por el
        # primer pago (300+900=1200 > 1000 real, aunque cada pago por
        # separado sea < 1000).
        primera = self._pagar('300.00')
        segunda = self._pagar('900.00')
        self.assertEqual(primera.status_code, 302)
        self.assertEqual(segunda.status_code, 302)
        self.assertEqual(PagoCompra.objects.filter(compra=self.compra).count(), 1)
        self.compra.refresh_from_db()
        self.assertEqual(self.compra.saldo_pendiente, Decimal('700.00'))

    def test_pago_fallido_no_dejo_registros_parciales_atomic(self):
        # Un sobrepago rechazado no debe dejar ni PagoCompra ni
        # MovimientoFinanciero huérfanos: confirma que
        # transaction.atomic() revierte todo el bloque junto.
        self._pagar('5000.00')
        self.assertEqual(PagoCompra.objects.count(), 0)
        self.assertEqual(MovimientoFinanciero.objects.count(), 0)

class CuentaPorPagarFinanzasTest(SistemaBaseTest):
    """Integración con Finanzas: la pestaña CxP aparece según permiso,
    y la compra a crédito no aparece como ingreso ni gasto contado."""

    def setUp(self):
        super().setUp()
        self.proveedor = Proveedor.objects.create(nombre='Proveedor CxP')
        self.compra = Compra.objects.create(
            proveedor=self.proveedor, numero_factura='F-200',
            subtotal=Decimal('500.00'), total=Decimal('500.00'),
            condicion_pago='Credito', estado_pago='Pendiente',
            fecha_vencimiento=timezone.now().date() + timedelta(days=15),
            creado_por=self.user,
        )

    def test_pestana_cxp_aparece_con_permiso_ver_compras(self):
        response = self.client.get(reverse('finanzas'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-tab="cxp"')

    def test_pestana_cxp_no_aparece_sin_permiso_ver_compras(self):
        grupo = self.crear_rol('Solo Finanzas General CxP', 'ver_finanzas')
        usuario = User.objects.create_user(username='finanzas_general_cxp', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        response = self.client.get(reverse('finanzas'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'data-tab="cxp"')

    def test_compra_credito_pendiente_aparece_en_listado_de_finanzas(self):
        response = self.client.get(reverse('finanzas') + '?tab=cxp')
        self.assertContains(response, self.proveedor.nombre)

class CuentaPorPagarSeguridadTest(SistemaBaseTest):
    """Permiso independiente 'registrar_pagos_compra': separado de
    'gestionar_compras', igual que 'registrar_cobros' es independiente
    de 'ver_finanzas' en CxC."""

    def setUp(self):
        super().setUp()
        self.proveedor = Proveedor.objects.create(nombre='Proveedor CxP')
        self.compra = Compra.objects.create(
            proveedor=self.proveedor, numero_factura='F-300',
            subtotal=Decimal('500.00'), total=Decimal('500.00'),
            condicion_pago='Credito', estado_pago='Pendiente',
            fecha_vencimiento=timezone.now().date() + timedelta(days=15),
            creado_por=self.user,
        )

    def test_usuario_sin_permiso_no_puede_ver_detalle_de_compra(self):
        usuario = User.objects.create_user(username='sin_permiso_cxp', password='Pass12345')
        self.client.force_login(usuario)
        response = self.client.get(reverse('compras_detalle', args=[self.compra.pk]))
        self.assertEqual(response.status_code, 403)

    def test_usuario_sin_permiso_no_puede_registrar_pago(self):
        usuario = User.objects.create_user(username='sin_permiso_cxp2', password='Pass12345')
        self.client.force_login(usuario)
        response = self.client.post(
            reverse('compras_registrar_pago', args=[self.compra.pk]),
            {'monto': '50.00', 'fecha': timezone.now().date(), 'metodo_pago': 'Efectivo'},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(PagoCompra.objects.count(), 0)

    def test_usuario_con_gestionar_compras_pero_sin_registrar_pagos_no_puede_pagar(self):
        # Confirma la separación de permisos pedida: gestionar_compras
        # (crear compras) ya NO habilita registrar pagos.
        grupo = self.crear_rol('Solo Gestiona Compras', 'ver_compras', 'gestionar_compras')
        usuario = User.objects.create_user(username='gestor_compras', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        puede_ver = self.client.get(reverse('compras_detalle', args=[self.compra.pk]))
        no_puede_pagar = self.client.post(
            reverse('compras_registrar_pago', args=[self.compra.pk]),
            {'monto': '50.00', 'fecha': timezone.now().date(), 'metodo_pago': 'Efectivo'},
        )
        self.assertEqual(puede_ver.status_code, 200)
        self.assertEqual(no_puede_pagar.status_code, 403)
        self.assertEqual(PagoCompra.objects.count(), 0)

    def test_usuario_autorizado_si_tiene_ver_compras_y_registrar_pagos_compra(self):
        grupo = self.crear_rol(
            'Pagador Proveedores', 'ver_compras', 'registrar_pagos_compra',
        )
        usuario = User.objects.create_user(username='pagador', password='Pass12345')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        puede_ver = self.client.get(reverse('compras_detalle', args=[self.compra.pk]))
        puede_pagar = self.client.post(
            reverse('compras_registrar_pago', args=[self.compra.pk]),
            {'monto': '50.00', 'fecha': timezone.now().date(), 'metodo_pago': 'Efectivo'},
        )
        self.assertEqual(puede_ver.status_code, 200)
        self.assertEqual(puede_pagar.status_code, 302)
        self.assertEqual(PagoCompra.objects.count(), 1)

class CuentaPorPagarCompatibilidadCxCTest(SistemaBaseTest):
    """CxP no debe alterar en absoluto el comportamiento de CxC: se
    ejercita el flujo completo de CxC de nuevo, tras la existencia de
    CxP en el mismo proyecto, para confirmar que ambos módulos
    conviven sin interferencia."""

    def test_flujo_cxc_completo_sigue_intacto_junto_a_cxp(self):
        response = self.client.post(
            reverse('procesar_venta'),
            data=json.dumps({
                'productos': [{'sku': self.producto.sku, 'cantidad': 2}],
                'cliente_id': self.cliente.id,
                'condicion_pago': 'Credito',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        cuenta = CuentaPorCobrar.objects.get()
        self.assertEqual(MovimientoFinanciero.objects.count(), 0)

        cobro_response = self.client.post(
            reverse('cuenta_por_cobrar_registrar_cobro', args=[cuenta.pk]),
            {'monto': '100.00', 'fecha': timezone.now().date(), 'metodo_pago': 'Efectivo'},
        )
        self.assertEqual(cobro_response.status_code, 302)
        cobro = Cobro.objects.get()
        movimiento = MovimientoFinanciero.objects.get()
        self.assertEqual(movimiento.tipo, 'Ingreso')
        self.assertEqual(movimiento.cobro_id, cobro.pk)
        self.assertIsNone(movimiento.pago_compra_id)
