# Reconstrucción por plazos: validación local

## Alcance

Nueva presentación por días, semanas y largo plazo. No hay cambios en
`StrategyConfig`, indicadores, puntuaciones, reglas de entrada o stops. No se
modifican operaciones reales, preferencias de correo o destinatarios. El panel
anterior continúa disponible en Herramientas. No se activan ni reinician
experimentos virtuales automáticamente.

La firma congelada de crecimiento con cohorte verificada sigue siendo:

`8200fd930b8c8eab529917ea29bd922b32ecc791785f3c61cbe4f699b88f5f11`

La firma técnica de referencia es distinta por diseño; no debe compararse con
la firma de crecimiento como si ambas representaran el mismo experimento.

## Pruebas

Validación final local: **2017 pruebas aprobadas** en 143,65 s, además de
compilación de `app.py`, módulos y scripts, y `git diff --check`. Los 2447 avisos
son advertencias de la suite, principalmente deprecaciones ya existentes; no
se han ocultado errores para conseguir un resultado verde.

- Inicio, Invertir y Resultados retornan antes de descargas, preparación del
  mercado, conciliación de ambas cuentas y persistencia de señales.
- Contexto enlazado a identidad, ticker, sesión y registro exactos. Rechazo de
  notas corruptas, duplicados ambiguos, información futura y señales anteriores
  al cierre.
- Los días son operaciones de varias sesiones, no intradía. Cada estrategia
  conserva sus reglas; una lectura técnica auxiliar no confirma una salida de
  una posición de calidad o de crecimiento.
- Datos ausentes quedan pendientes, no como ceros ni ventas. La investigación
  de negocios a largo plazo no se presenta como compra confirmada.
- Lecturas con propietario, sello y límites; caché con TTL e invalidación.
  Enlaces de informes archivados, controles anteriores y cambio de usuario.
- Resultados de A–D separados por experimento, enfoque, cohorte y modalidad.
  Se ha probado también con activación y avance reales sobre SQLite temporal,
  no sólo con respuestas falsas del almacenamiento. Consultar no altera el
  registro ni las operaciones.
- Compatibilidad de despliegue en caliente: dependencias reparadas antes de
  importar las nuevas vistas; sin limpiar datos privados ni necesitar red.

## Fluidez medida

AppTest local, Python 3.12.14 y Streamlit 1.66.1. Informe **sintético** de 155
empresas: 25 posiciones, 100 candidaturas y 30 en espera. Cinco ejecuciones por
pantalla tras el calentamiento, sin otro pytest ejecutándose en paralelo.
Son tiempos del renderizador en servidor local, no del navegador o la nube.

| Vista | Mediana local | Muestras (ms) |
| --- | ---: | --- |
| Inicio | 26,65 ms | 27,02; 26,70; 26,42; 26,39; 26,65 |
| Invertir / Semanas | 33,27 ms | 32,96; 33,84; 33,89; 33,27; 32,95 |

Inicio agrupa los planes no incluidos en un solo aviso; no crea 25 tarjetas
idénticas. Invertir limita tarjetas y renderiza una empresa adicional elegida
por grupo, incluso dentro de desplegables que Streamlit ejecuta con antelación.
En el ensayo de Semanas se renderizaron 13 tarjetas, no 155.

Este probe usa el informe proporcionado en memoria y no incluye la latencia
de leerlo en Supabase, ni la unión del contexto técnico, descargas o cálculos
pedidos explícitamente. Las pruebas de navegación verifican aparte el número
acotado de lecturas y que los motores no se ejecuten al navegar.

## Límites pendientes

- No se ha validado aquí una sesión privada en navegador de producción.
- No hay prueba de rentabilidad por mejorar la presentación. El laboratorio
  necesita sesiones prospectivas completas y flujos comparables.
- Resultados consulta sólo las cuatro temporadas seleccionadas, pero la API
  actual del registro devuelve su histórico antes del límite defensivo de
  1000 registros. No se consulta al abrir la pantalla ni al cambiar filtros.
- Los cambios de plan se aplican a la fotografía guardada; un informe ya
  sellado conserva el plan que conocía, sin reescribir su pasado.

No se publican en este archivo posiciones, saldos, destinatarios ni informes
privados. Las pruebas no envían correos ni realizan operaciones reales.
