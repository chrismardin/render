# Arquitectura de Cromf Finanzas

## Vista general

Cromf Finanzas es una aplicacion Django monolitica con una aplicacion principal, `core`, y una configuracion de proyecto en `mi_proyecto_django`. La base de datos configurada actualmente es SQLite en `db.sqlite3`.

```text
mi_proyecto_django/
  settings.py
  urls.py
  asgi.py
  wsgi.py
core/
  models.py
  views.py
  forms.py
  urls.py
  permisos.py
  permisos_django.py
  roles_permisos.py
  recuperacion.py
  context_processors.py
  templates/core/
  static/css/
  static/js/
  migrations/
  management/commands/
media/reportes/
db.sqlite3
```

## Capas

- **Modelos:** representan productos, movimientos de stock, movimientos financieros, clientes, ventas, empleados, reportes, configuracion, preferencias, proveedores, ordenes, recepciones, compras y solicitudes de recuperacion.
- **Formularios:** encapsulan validaciones y widgets Django para operaciones de productos, ventas, finanzas, inventario, usuarios, roles, recuperacion y compras.
- **Vistas:** reciben peticiones, validan permisos, coordinan formularios y consultas, actualizan modelos y renderizan plantillas.
- **Plantillas:** presentan la informacion y conservan los nombres de campos, variables de contexto, URLs y acciones funcionales.
- **Static:** contiene los estilos compartidos, el sidebar, login y los scripts de interaccion.
- **Migraciones:** mantienen el esquema y las migraciones de datos de Django.
- **Comandos:** cargan datos base y preparan permisos y roles.

## Autenticacion y autorizacion

Django gestiona `User`, sesiones, login, logout, hashing de contrasenas y proteccion CSRF. Las vistas protegidas usan `login_required` y los permisos de modulo se comprueban con las utilidades del proyecto.

La autorizacion sigue esta relacion:

```text
User -> Group -> Permission -> Modulo
```

Los permisos se registran en `auth_permission`, se agrupan mediante `Group` y se consultan desde `permisos_django.py`, `permisos.py` y `roles_permisos.py`. Esto permite que los roles y permisos se administren dinamicamente sin depender de un nombre de usuario especifico.

La recuperacion utiliza `SolicitudRecuperacionPassword`: una solicitud puede ser revisada por un administrador con permiso, y solo despues el usuario establece la nueva contrasena con las herramientas nativas de Django.

## Integracion de modulos

La venta utiliza `Producto`, `Cliente`, `Venta` y `DetalleVenta`. Al procesarse, actualiza el stock mediante `MovimientoStock` y registra el ingreso mediante `MovimientoFinanciero`.

Las compras siguen este flujo:

```text
Proveedor -> OrdenCompra -> Recepcion -> MovimientoStock
Compra -> MovimientoFinanciero
```

Crear una orden no cambia el inventario. Registrar una recepcion actualiza el stock con movimientos de entrada. Una compra al contado se refleja financieramente; una compra a credito conserva un saldo pendiente hasta que se registra el pago.

Finanzas tambien muestra la nomina calculada de empleados activos como gasto virtual para evitar duplicarla en la base de datos cada vez que se carga una pagina.

## Frontend y CSS

Las plantillas extienden `core/templates/core/base.html`, que carga las hojas compartidas y contiene el sidebar. El sidebar tiene su logica de estado activo en la plantilla base y su presentacion en `sidebar.css`.

La organizacion CSS es intencional:

- `styles.css`: layout principal, topbar, tab bars, tarjetas KPI, paneles, tablas, formularios, alertas y componentes compartidos.
- `sidebar.css`: exclusivamente sidebar y navegacion lateral.
- `login.css`: exclusivamente login y pantallas publicas de recuperacion que usan el layout de acceso.

Las subvistas reutilizan el tab bar compartido. Los iconos de la interfaz se representan con SVG inline outline y `currentColor`, sin dependencias externas ni emojis decorativos.

## Datos y despliegue

El proyecto actual usa SQLite y archivos locales en `media/reportes`. No tiene una configuracion activa de PostgreSQL/Neon. Para desplegarlo en otro entorno se deben externalizar la clave secreta, debug, hosts permitidos y credenciales de base de datos, sin subir secretos al repositorio.

Los pasos basicos son crear `.venv`, instalar `requirements.txt`, configurar el entorno, ejecutar `migrate`, crear un superusuario si corresponde y levantar Django con `runserver` durante el desarrollo.
