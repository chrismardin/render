# Cromf Finanzas

ERP financiero y empresarial desarrollado con Django. Esta versión usa **Chris como base**, incorpora las implementaciones mejores/nuevas de **Mónica actual** y conserva las funcionalidades nuevas de **Rosa** sin reemplazar la semántica/layout principal por la estructura de Rosa.

## Módulos visibles

El sidebar conserva exactamente estos 12 módulos:

1. Dashboard
2. Productos
3. Ventas
4. Finanzas
5. Inventario
6. Proyecciones
7. Clientes
8. RRHH
9. Impuestos
10. Reportes
11. Modo IA
12. Configuración

Compras continúa accesible desde Inventario; Usuarios/Roles/Recuperación permanecen como herramientas internas de Configuración.

## Base de datos y `.env`

El proyecto carga variables desde un archivo `.env` situado junto a `manage.py`.

- Si existe `DATABASE_URL`, `settings.py` usa esa conexión (por ejemplo PostgreSQL/Neon).
- Si no existe `DATABASE_URL`, mantiene un fallback local SQLite para desarrollo.
- El `.env` real **no forma parte del ZIP** y nunca debe subirse con credenciales.
- Se incluye `.env.example` únicamente como referencia sin secretos.

Antes de migrar contra la base real, coloca tu `.env` y verifica el motor:

```powershell
python manage.py shell -c "from django.conf import settings; print(settings.DATABASES['default']['ENGINE']); print(settings.DATABASES['default']['NAME'])"
```

## Principales integraciones de Mónica actual

- CxC con `CuentaPorCobrar` + `Cobro`, pagos parciales e historial.
- CxP sobre `Compra` + `PagoCompra`, pagos parciales e historial.
- Condición de pago separada del método de pago.
- Factura interna `FAC-...`, snapshot del cliente, descuentos, vendedor y observaciones.
- Pedidos/ventas y compras con mayor trazabilidad.
- RRHH con Empleados, Nómina, Ausencias y Feriados.
- Desactivación lógica de empleados para preservar históricos.
- Nómina mensual persistente mediante `Nomina` + `DetalleNomina`.
- Motor de cálculo separado en `core/servicios_nomina.py`.
- `TramoISR` y `TopeTSS` versionados por año.
- Pago colectivo de nómina con un único `MovimientoFinanciero`, transacción y bloqueo de fila.
- PDF de nómina y recibo individual HTML/PDF.
- Permisos separados para Empleados, Nómina, Ausencias, Feriados y pago de nómina.

## Funcionalidades de Rosa conservadas

Se conservan como complemento, adaptadas a la estructura semántica/base de Chris/Mónica:

- Producto con precio base sin ITBIS y opción de exención.
- Condición fiscal del cliente.
- Comprobantes E31/E32/E44.
- Cuentas financieras Banco/Caja/Tarjeta.
- Módulo Impuestos.
- Modo IA, historial `ConsultaIA` y predicción local.
- Resumen administrativo de RRHH, reutilizando el motor configurable de nómina de Mónica.

No se incorporó la estructura HTML repetida de Rosa, su sistema antiguo `PermisoPagina`, su CxC/CxP simple, ni el JavaScript de reinicio de ventas, porque Chris/Mónica ya tienen implementaciones más seguras o completas.

## Estructura principal

```text
mi_proyecto_django/
  settings.py
  urls.py
core/
  models.py
  views.py
  forms.py
  urls.py
  servicios_nomina.py
  permisos_django.py
  roles_permisos.py
  templates/core/
  static/css/
  static/js/
  migrations/
  management/commands/
  ml/
```

## Migraciones de esta versión

La integración previa Chris + Mónica/Rosa está en:

```text
core/migrations/0026_integracion_monica_rosa_funcional.py
```

La nómina de Mónica actual y sus permisos/configuración se agregan en:

```text
core/migrations/0027_nomina_monica_actual.py
```

No se reescriben las migraciones anteriores.

## Instalación

```powershell
python -m venv venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Después coloca tu `.env` junto a `manage.py` y ejecuta:

```powershell
python manage.py migrate
python manage.py check
python manage.py runserver
```

## Modo IA

El entrenamiento local se ejecuta con:

```powershell
python manage.py entrenar_modelo_ia
```

`ANTHROPIC_API_KEY` es opcional y se lee desde `.env`. Los archivos de modelos generados (`*.pkl`) no deben incluirse en el proyecto distribuido.

## Nota sobre Control Diario de Asistencia

El documento más reciente de Mónica describe un **bloque futuro** de Control Diario de Asistencia. No se implementó en esta fusión porque todavía no forma parte del ZIP funcional de Mónica actual. Cuando venga implementado y validado en una versión posterior, se podrá integrar siguiendo la misma prioridad arquitectónica.

## Integración Rosa nuevo (sin reemplazar semántica principal)

Se incorporaron únicamente novedades funcionales que no estaban ya resueltas mejor en el proyecto final:

- Tipo de nómina por empleado: Mensual / Quincenal.
- Snapshot de primera y segunda quincena dentro del detalle mensual de nómina, sin reemplazar el motor de nómina de Mónica ni duplicar corridas.
- Recibos de ingresos desde Finanzas, con vista HTML, impresión, compartir y PDF.
- Retención informativa de tarjetas del 2% dentro del módulo de Impuestos.
- Pre-607 en TXT y PDF de revisión interna. No se presenta como archivo oficial DGII ni fabrica e-NCF autorizados.
- Resumen 606 alimentado por el módulo de Compras/Proveedores existente. Se mantiene como resumen interno porque el ERP no captura todavía todos los campos de un 606 oficial.
- Compartir factura desde la pantalla de factura existente.

No se reemplazaron `base.html`, `styles.css`, `sidebar.css`, `dashboard.html` ni `ventas.html` con archivos de Rosa. La estructura/semántica principal se mantiene.

### Migración nueva

`core/migrations/0031_rosa_nuevo_novedades.py`

Después de colocar el `.env` junto a `manage.py`:

```powershell
python manage.py migrate
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py runserver
```

## Rendimiento durante desarrollo local

El proyecto puede trabajar con PostgreSQL remoto mediante `DATABASE_URL`. Cuando la base está en Internet, cada consulta añade latencia y el servidor de desarrollo puede sentirse más lento que SQLite.

- Para conservar la base remota y reutilizar mejor la conexión durante pruebas locales, ejecutar: `./runserver_rapido.ps1`.
- Para máxima velocidad local, ejecutar: `./runserver_sqlite_rapido.ps1`. Este modo fuerza `db.sqlite3`; sus datos son independientes de los de PostgreSQL/Neon.
- También se puede activar SQLite manualmente en PowerShell con `$env:USE_LOCAL_SQLITE="1"` antes de `python manage.py runserver`.

Finanzas fue optimizado para cargar únicamente la sección visible (`Ingresos`, `Gastos`, `Historial`, `CxC`, `CxP` o `Cuentas`) en vez de consultar todas las secciones en cada visita. Los ingresos de Finanzas son de solo lectura: se generan desde Ventas y cobros de CxC; el registro manual queda disponible únicamente para gastos operativos.


## Rendimiento local (Neon)

Para desarrollar usando la base PostgreSQL remota, se incluye `runserver_rapido.ps1`.
Este arranque desactiva el autoreloader y conserva un único hilo para favorecer la
reutilización de la conexión persistente.

```powershell
.\venv\Scripts\Activate.ps1
.\runserver_rapido.ps1
```

El proyecto también evita consultas N+1 en Productos/Inventario y reduce consultas
repetidas de RRHH. Para producción, la mayor mejora es desplegar Django cerca de la
región de la base de datos; ejecutar Django localmente y consultar una base remota
siempre añade latencia de red a cada consulta SQL.

## Rendimiento con PostgreSQL / Neon

Esta versión mantiene PostgreSQL/Neon como base principal y optimiza el número
de consultas. No se eliminó ninguna funcionalidad ni se añadió caché de datos
contables que pueda mostrar cifras atrasadas.

Cambios principales de rendimiento:

- Dashboard: reutiliza la nómina virtual y agrupa KPIs en PostgreSQL.
- Reportes: gráficos mensuales se obtienen con consultas agrupadas en vez de
  varias consultas por cada mes.
- Proyecciones: PostgreSQL agrupa ventas por producto/mes; ya no se descargan
  todos los detalles de 90 días para sumarlos en Python.
- RRHH/Nómina: DatosEmpresa, TopeTSS, TramoISR y ausencias se cargan una sola
  vez por cálculo, no una vez por empleado.
- Nómina: los detalles se insertan en bloque manteniendo el mismo snapshot.
- Se agregan índices para los filtros más usados de Ventas, Finanzas,
  Inventario, Empleados y Ausencias.
- `python manage.py runserver` usa un solo hilo en desarrollo para que la
  conexión persistente a Neon pueda reutilizarse. Esto solo afecta al servidor
  de desarrollo; un despliegue real usa el servidor del hosting.

Después de actualizar el proyecto:

```powershell
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py check
python manage.py runserver
```

No es necesario usar `runserver_rapido.ps1`; se conserva únicamente por
compatibilidad con versiones anteriores del proyecto.
