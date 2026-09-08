import os
import pandas as pd
import joblib
from datetime import timedelta
from collections import defaultdict
from django.utils.timezone import now
from sklearn.ensemble import RandomForestRegressor
from sklearn.preprocessing import LabelEncoder

from core.models import DetalleVenta, Producto

RUTA_MODELO = os.path.join(os.path.dirname(__file__), 'modelo_demanda.pkl')
RUTA_ENCODER = os.path.join(os.path.dirname(__file__), 'encoder_categoria.pkl')


def construir_dataset():
    """
    Arma un dataset a nivel de PRODUCTO x DIA, sumando cuantas unidades
    se vendieron de cada producto en cada dia. Esto da mas filas de
    entrenamiento que agrupar por mes, algo clave cuando el historial
    de ventas todavia es corto.
    """
    detalles = DetalleVenta.objects.select_related('producto', 'venta').all()

    filas = defaultdict(int)
    info_producto = {}

    for d in detalles:
        fecha = d.venta.fecha.date()
        clave = (d.producto_id, fecha)
        filas[clave] += d.cantidad
        info_producto[d.producto_id] = d.producto

    registros = []
    for (producto_id, fecha), cantidad in filas.items():
        producto = info_producto[producto_id]
        registros.append({
            'producto_id': producto_id,
            'categoria': producto.categoria,
            'precio_venta': float(producto.precio_venta),
            'dia_semana': fecha.weekday(),
            'mes': fecha.month,
            'cantidad': cantidad,
        })

    return pd.DataFrame(registros)


def entrenar_modelo():
    """
    Entrena el RandomForestRegressor con el historial de ventas real.
    Guarda el modelo entrenado y el encoder de categorias en disco (.pkl)
    para no tener que reentrenar en cada request.
    Devuelve un diccionario con metricas basicas del entrenamiento.
    """
    df = construir_dataset()

    if len(df) < 5:
        return {
            'exito': False,
            'mensaje': f'Muy pocos datos para entrenar ({len(df)} registros). '
                       f'Se necesitan al menos 5 dias con ventas registradas.',
        }

    encoder_categoria = LabelEncoder()
    df['categoria_cod'] = encoder_categoria.fit_transform(df['categoria'])

    caracteristicas = ['categoria_cod', 'precio_venta', 'dia_semana', 'mes']
    X = df[caracteristicas]
    y = df['cantidad']

    modelo = RandomForestRegressor(
        n_estimators=100,
        max_depth=6,
        random_state=42,
    )
    modelo.fit(X, y)

    joblib.dump(modelo, RUTA_MODELO)
    joblib.dump(encoder_categoria, RUTA_ENCODER)

    importancias = dict(zip(caracteristicas, modelo.feature_importances_.round(3)))

    return {
        'exito': True,
        'registros_entrenamiento': len(df),
        'productos_distintos': df['producto_id'].nunique(),
        'importancia_variables': importancias,
    }


def cargar_modelo():
    if not os.path.exists(RUTA_MODELO) or not os.path.exists(RUTA_ENCODER):
        return None, None
    modelo = joblib.load(RUTA_MODELO)
    encoder_categoria = joblib.load(RUTA_ENCODER)
    return modelo, encoder_categoria


def predecir_demanda_producto(producto, modelo, encoder_categoria, dias=30):
    """
    Predice la demanda diaria promedio de un producto para los proximos
    `dias` dias, usando el modelo ya entrenado. Si la categoria del
    producto no fue vista durante el entrenamiento, se omite (retorna 0).

    Optimizado: arma un solo DataFrame con todos los dias del horizonte
    y hace UNA sola llamada a modelo.predict(), en vez de una llamada
    por cada dia (mucho mas rapido).
    """
    try:
        categoria_cod = encoder_categoria.transform([producto.categoria])[0]
    except ValueError:
        return 0.0

    hoy = now().date()
    fechas_futuras = [hoy + timedelta(days=i) for i in range(dias)]

    filas = pd.DataFrame([{
        'categoria_cod': categoria_cod,
        'precio_venta': float(producto.precio_venta),
        'dia_semana': f.weekday(),
        'mes': f.month,
    } for f in fechas_futuras])

    predicciones = modelo.predict(filas)
    predicciones = [max(0, p) for p in predicciones]

    return sum(predicciones) / len(predicciones) if predicciones else 0.0


def generar_predicciones_productos(dias_horizonte=30):
    """
    Genera, para cada producto activo, la demanda diaria promedio
    proyectada, la proyeccion a `dias_horizonte` dias, y si necesita
    reabastecimiento pronto segun su stock actual.
    """
    modelo, encoder_categoria = cargar_modelo()
    if modelo is None:
        return {
            'disponible': False,
            'mensaje': 'El modelo de IA todavia no ha sido entrenado. '
                       'Ejecuta: python manage.py entrenar_modelo_ia',
        }

    resultados = []
    # Solo mercancía destinada a venta participa en las predicciones.
    # Los materiales/insumos internos del módulo Compras no deben aparecer
    # como demanda comercial.
    for producto in Producto.objects.filter(tipo_articulo='Mercancia'):
        demanda_diaria = predecir_demanda_producto(producto, modelo, encoder_categoria, dias=dias_horizonte)
        proyeccion_total = round(demanda_diaria * dias_horizonte)

        if demanda_diaria > 0:
            dias_hasta_agotarse = round(producto.stock / demanda_diaria, 1)
        else:
            dias_hasta_agotarse = None

        necesita_reabastecer = (
            dias_hasta_agotarse is not None and dias_hasta_agotarse <= 14
        ) or producto.stock < producto.stock_minimo

        cantidad_sugerida = 0
        if necesita_reabastecer:
            cantidad_sugerida = max(
                round(demanda_diaria * 30) - producto.stock,
                producto.stock_minimo,
            )

        resultados.append({
            'producto': producto,
            'demanda_diaria_promedio': round(demanda_diaria, 2),
            'proyeccion_30_dias': proyeccion_total,
            'dias_hasta_agotarse': dias_hasta_agotarse,
            'necesita_reabastecer': necesita_reabastecer,
            'cantidad_sugerida_compra': cantidad_sugerida,
        })

    resultados.sort(key=lambda x: x['proyeccion_30_dias'], reverse=True)

    return {
        'disponible': True,
        'productos': resultados,
        'alertas_reabastecimiento': [r for r in resultados if r['necesita_reabastecer']],
    }


def generar_resumen_negocio():
    """
    Resumen general para las tarjetas KPI del dashboard de Modo IA.
    """
    datos = generar_predicciones_productos()
    if not datos['disponible']:
        return datos

    productos = datos['productos']
    ingresos_proyectados = sum(
        r['proyeccion_30_dias'] * float(r['producto'].precio_venta) for r in productos
    )

    return {
        'disponible': True,
        'ingresos_proyectados_30_dias': round(ingresos_proyectados, 2),
        'top_5_productos': productos[:5],
        'total_alertas_reabastecimiento': len(datos['alertas_reabastecimiento']),
        'alertas_reabastecimiento': datos['alertas_reabastecimiento'][:10],
    }
