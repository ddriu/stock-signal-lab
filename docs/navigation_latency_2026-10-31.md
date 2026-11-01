# Fluidez: medición offline con cartera poblada

Mediciones realizadas al terminar la revisión del 31 de octubre, entre las
00:40 y las 00:54 UTC del 1 de noviembre de 2026. Referencia anterior: navegación
publicada en `2b810d1`, reproducida con datos offline. Comparación posterior:
fuentes locales con el helper integrado; blob de `app.py`:
`70d1a36aa9144f971d217ac77a28b33a3cf25bc2`. No se atribuyen estos cambios locales
al commit HEAD que pudiera mostrar el directorio de trabajo.

Entorno: Python 3.12.14, pandas 2.3.3 y Streamlit 1.66.1. No se ha consultado
producción ni enviado correo. La medición posterior se repitió aislada, sin
ejecutar pytest en paralelo para evitar interferencias de CPU.

## Qué se ha medido

`tests/test_navigation_latency.py` utiliza AppTest y los renderizadores reales de
Inicio, Favoritos, Cartera y Crecimiento y momentum. También ejecuta los
indicadores, señales y política reales detrás de `_analysis_view_context`.
No sustituye estos motores por respuestas vacías.

La sesión contiene 30 empresas con 520 sesiones OHLCV cada una, tres referencias,
150 favoritas privadas, 30 favoritas de grupo y 180 compras que mantienen 18
posiciones abiertas. El almacenamiento es un objeto sintético con **50 ms de
latencia por lectura no cacheada**. Se usa el `SessionJournal` real. Los datos
de mercado ya están en la sesión; este ensayo no mide su descarga inicial.

Se bloquean expresamente requests, SMTP, actualizaciones de mercado, persistencia
de análisis y métodos de mutación del diario. No hay base de datos, credenciales
ni posiciones reales disponibles en el ensayo.

Las cifras son tiempo de ejecución de AppTest en servidor local, no tiempo de
pintado del navegador, latencia de Internet ni SLA de Streamlit Community Cloud.
Se toman tres muestras por ruta en la misma sesión y se publica su mediana (p50).

## Referencia anterior

Primer arranque de sesión: **2,656 s**, incluidos importación, preparación del
contexto, primeras lecturas sintéticas y renderizado de Inicio.

| Ruta con sesión caliente | p50 | Lecturas nuevas en las tres muestras | Cálculo repetido observado por render |
| --- | ---: | ---: | --- |
| Inicio / Resumen | 814 ms | 0 | Dos dashboards; dos conciliaciones en la ruta de respaldo |
| Favoritos / Mis listas | 15 ms | 0 | Listas y editores visibles, sin motor |
| Cartera / Posiciones | 65 ms | 0 | 18 señales por posición, un estudio histórico y un dashboard |
| Cartera / Resultado real | 58 ms | 0 | Informe de resultados y formulario visibles |
| Cartera / Historial | 42 ms | 0 | Tabla y controles del historial visibles |
| Cartera / Evolución | 218 ms | 1 | Historia, gráficos, Excel y un dashboard |
| Crecimiento y momentum | 202 ms | 0 | 30 evaluaciones de crecimiento específicas del panel |

En las siete rutas, `prepare_data` y `_enrich_current_analysis_policy` se
ejecutan **cero veces adicionales** después de construir el contexto inicial.
La única lectura nueva de Evolución es la primera consulta de cuentas; después
se reutiliza el resultado. La TTL usada en el probe es de 600 s para que la
expiración no contamine una comparación entre rutas; la TTL normal del proxy es
de 60 s y ya tiene pruebas específicas de expiración.

Los tiempos acumulados instrumentados también muestran que las 90 evaluaciones
de crecimiento de tres renders consumen 104 ms en total. Las 54 señales de
posiciones consumen 23 ms y sus tres estudios históricos, 4 ms. No son el cuello
de botella principal en este conjunto de tamaño moderado.

## Comparación después de integrar la reutilización de conciliación

Se repite exactamente el mismo fixture, la misma latencia sintética y tres
muestras por ruta. Son medianas locales de renderizado de servidor:

| Ruta | Antes | Después | Observación |
| --- | ---: | ---: | --- |
| Inicio / Resumen | 814 ms | **84 ms** | Aproximadamente 90% menos tiempo; objetivo de 250 ms cumplido |
| Favoritos / Mis listas | 15 ms | 15 ms | Sin variación relevante |
| Cartera / Posiciones | 65 ms | 66 ms | Sin variación relevante |
| Cartera / Resultado real | 58 ms | 59 ms | Sin variación relevante |
| Cartera / Historial | 42 ms | 39 ms | Variación pequeña entre ejecuciones |
| Cartera / Evolución | 218 ms | 218 ms | Sin variación relevante |
| Crecimiento y momentum | 202 ms | 206 ms | Mantiene sus evaluaciones propias |

Las tres muestras de Inicio después del cambio fueron 115, 84 y 84 ms.
El primer arranque completo observado fue 2,275 s frente a 2,656 s anteriormente;
como sólo se tomó una muestra fría por ensayo, no se atribuye esa diferencia
al helper ni se presenta como una mejora fría estadísticamente demostrada.

Se mantienen cero descargas, llamadas SMTP y mutaciones. También se mantienen
cero ejecuciones adicionales de preparación/enriquecimiento general durante la
navegación caliente. Las lecturas no cacheadas fueron cero, salvo la primera
consulta de cuentas al abrir Evolución. Los dashboards ligeros de Inicio siguen
calculándose; la conciliación costosa se reutiliza mientras sus entradas sean
idénticas.

## Hallazgo prioritario: conciliación de Inicio

Un segundo ensayo con cProfile, separado de las medianas anteriores porque el
perfilador introduce sobrecoste, mide 1,044 s dentro de `render_home` caliente.
Las dos llamadas a `reconcile_current_portfolio` acumulan 0,901 s. Se observan
455 llamadas a `pandas.to_datetime`, de las que 365 pasan por inferencia de
formato; esta inferencia acumula 0,685 s.

Por tanto, no conviene atribuir los 814 ms de Inicio a descargas o al motor de
mercado: ambos se reutilizan correctamente en este ensayo. El trabajo pendiente
más rentable es evitar reconstruir dos veces la misma conciliación de respaldo
y evitar parsear las mismas fechas por operación. Cualquier cambio deberá
conservar las reglas contables y sus pruebas. No se ha modificado el parsing:
la mejora posterior sólo reutiliza su resultado.

### Mejora integrada localmente y validada

Se ha añadido `src/portfolio_view_context.py` con `reconcile_home_views`. Mantiene
las dos llamadas al mismo reconciliador, pero reutiliza sus resultados por
propietario y sesión cuando no han cambiado fotografía, valoración de mercado,
operaciones ni dashboard. También invalidan la revisión explícita del diario,
el día —que ya influía en el reconciliador original— y la versión del helper.
Los resultados y objetos mutables están aislados mediante copias.

Sus pruebas, junto con las de caché genérica, superan **41 casos** y comparan el
resultado con el reconciliador real sin modificar reglas ni parsing de fechas.
La integración en `render_home` se realizó en el cambio separado de la aplicación;
este informe repite después la medición con ese código. Se amplió a cuatro la
capacidad de vistas para que el contexto de análisis y la conciliación no se
expulsen mutuamente. Las cifras anteriores demuestran la mejora en el fixture
offline; no constituyen una validación de velocidad en producción.

## Prioridades y límites

1. Validar en un navegador y entorno desplegado la mejora ya medida offline,
   incluyendo invalidación al cambiar operaciones, fotografía, precios o FX.
   El objetivo orientativo local de p50 caliente inferior a 250 ms está cumplido
   en este fixture, no verificado todavía como SLA de producción.
2. Evitar la segunda evaluación de crecimiento del panel si consume exactamente
   el mismo resultado versionado. Mantener independientes sus reglas; no mezclar
   estrategias ni cambiar puntuaciones para mejorar un tiempo de respuesta.
3. Cachear resultados de visualización de Evolución y el backtest seleccionado
   sólo si una medición con historiales mayores demuestra que merece la pena.
   El backtest automático y páginas no incluidas en la tabla no tienen aquí un
   tiempo medido ni una afirmación de fluidez.

El probe tiene posiciones y operaciones pobladas, pero no una fotografía
importada, lotes de importación ni alternativas privadas pobladas. Por ello
documenta específicamente la ruta contable de respaldo de Inicio y no demuestra
el rendimiento de todas las variantes de conciliación. Tampoco simula cuotas
de proveedor, CPU compartida de producción, 150 historiales de mercado cargados,
sesiones simultáneas o navegador móvil.

Las pruebas existentes de navegación general siguen siendo necesarias: ejercitan
`main`, cambios de propietario y controles. Este nuevo probe aísla renderizadores
reales para medir sus costes; no pretende reemplazar una prueba de extremo a
extremo en navegador.

## Reproducción

Desde la raíz del repositorio, usando el runtime de validación:

```bash
../validation-runtime/bin/python -m pytest -q \
  tests/test_navigation_latency.py tests/test_fast_navigation.py \
  tests/test_portfolio_view_context.py \
  tests/test_lazy_journal_sections.py tests/test_view_cache.py \
  tests/test_session_journal.py
```

Resultado posterior observado: **86 pruebas superadas en 7,48 s**. Las pruebas comprueban
reutilización, rutas visibles y ausencia de efectos externos, no fijan umbrales
absolutos de reloj que volverían inestable la CI.

Para repetir las medianas, sin red ni almacenamiento real:

```bash
../validation-runtime/bin/python - <<'PY'
import json, runpy
probe = runpy.run_path('tests/test_navigation_latency.py')
result = probe['benchmark_routes'](repeats=3, storage_latency=0.05)
print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
PY
```

La actualización horaria del mercado es un proceso independiente: este ensayo
no la activa, no la mide ni altera la planificación o los correos diarios.
