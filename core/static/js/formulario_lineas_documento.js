/*
  Mejora de presentación para los formularios con formset de líneas
  (Orden de Compra, Pedido de Venta): al elegir un producto, sugiere
  el precio/costo que ya existe en el catálogo (no lo obliga a
  aceptarlo, el campo sigue siendo editable), y muestra un total
  estimado en vivo mientras se completa el formulario. Ningún dato
  de negocio está escrito aquí: el catálogo se recibe del servidor
  vía json_script (ver plantilla), tal como ya hace ventas.js.

  No cambia qué se envía al servidor ni cómo se procesa el formulario:
  es puramente informativo en pantalla.
*/
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('[data-lineas-formset]').forEach(function (tabla) {
    var catalogo = {};
    var catalogoEl = document.getElementById(tabla.dataset.catalogo || '');
    if (catalogoEl) {
      try {
        JSON.parse(catalogoEl.textContent).forEach(function (p) {
          catalogo[String(p.id)] = p;
        });
      } catch (e) { /* catálogo vacío o inválido: se ignora el autocompletado */ }
    }

    var campoPrecio = tabla.dataset.campoPrecio || 'precio_unitario';
    var totalEl = tabla.dataset.totalTarget ? document.querySelector(tabla.dataset.totalTarget) : null;

    function campoDeFila(fila, sufijo) {
      return fila.querySelector('[name$="-' + sufijo + '"]');
    }

    function recalcularTotal() {
      if (!totalEl) return;
      var total = 0;
      tabla.querySelectorAll('tbody tr').forEach(function (fila) {
        var eliminar = campoDeFila(fila, 'DELETE');
        if (eliminar && eliminar.checked) return;
        var cantidadInput = campoDeFila(fila, 'cantidad_solicitada') || campoDeFila(fila, 'cantidad');
        var precioInput = campoDeFila(fila, campoPrecio);
        var cantidad = parseFloat(cantidadInput && cantidadInput.value) || 0;
        var precio = parseFloat(precioInput && precioInput.value) || 0;
        total += cantidad * precio;
      });
      totalEl.textContent = 'RD$ ' + total.toFixed(2);
    }

    tabla.addEventListener('change', function (ev) {
      var fila = ev.target.closest('tr');
      if (!fila || !ev.target.name) return;
      if (ev.target.name.indexOf('-producto') !== -1) {
        var datos = catalogo[String(ev.target.value)];
        var precioInput = campoDeFila(fila, campoPrecio);
        if (datos && precioInput && !precioInput.value) {
          precioInput.value = datos.precio;
        }
      }
      recalcularTotal();
    });
    tabla.addEventListener('input', recalcularTotal);
    recalcularTotal();
  });
});
