# Correo compacto y pausa del laboratorio — 7 de noviembre de 2026

La notificación de GitHub de `Medición prospectiva del modelo (sin correos)`
es distinta del resumen bursátil. La ejecución 42327766395 registró una recogida
atrasada y cero resultados nuevos. No hubo intento SMTP en ese proceso.

El historial permanece inmutable. El diagnóstico del CLI conserva el fallo y
explica la pausa: no se eliminan incidencias para aparentar recuperación.
Una nueva activación necesita auditoría retenida válida, configuración congelada,
fecha actual real y confirmación explícita. No hay compras históricas inventadas.
Después se selecciona su ID mediante el secreto `MODEL_RESULTS_EXPERIMENT` en el programador,
con `MODEL_RESULTS_OWNER` explícito. Activar otra temporada sin seleccionar su ID
no resuelve los avisos: la antigua seguiría detectándose como pausada. Se debe
verificar un ciclo de recogida antes de considerar recuperado el laboratorio.
La nueva selección no borra temporadas ni elige retrospectivamente un ganador.

## Presentación del resumen

- Mismo informe sellado para aplicación y envío; no se recalculan notas al formatear.
- Tabla de dos columnas, anchura flexible y hasta cinco empresas; cartera primero.
- Nota y cambio comparable en puntos; N/D y falta de comparación no equivalen a cero.
- Hasta tres posibles entradas fuertes verificadas en un bloque independiente.
- Las alertas no consumen la cuota de notas. Se conservan eventos, límites,
  fuente, fecha, estrategia, horizonte y siguiente paso.
- Se muestran hasta tres alertas; las restantes conservan recuento y empresas
  identificadas para consultar en el informe, sin desaparecer silenciosamente.
- Se retiran las cinco fechas por fila y los párrafos repetidos. El historial
  completo se consulta mediante el enlace al informe exacto.
- No se fusionan puntuaciones de estrategias distintas ni se cambia el motor.

No se cambian preferencias, destinatarios, reserva de entrega, deduplicación ni
la distinción entre aceptación SMTP y llegada a bandeja. La entrega automática
ya no llama al laboratorio; su ejecución independiente conserva su propio estado.

Vista previa sin datos personales ni transporte:

`python -m scripts.preview_compact_email /tmp/stock-signal-lab-correo-demo.html`

La herramienta BrowserAct y su requisito uv no están disponibles en esta sesión.
La prueba de estructura HTML no sustituye una inspección real en Gmail/iPhone.
No se afirma validación visual ni recepción real por superar pruebas locales.

## Validación local

`python -m pytest -q --tb=short --disable-warnings`: **2055 pruebas superadas**
en 150,33 s. Los 2447 avisos son advertencias, principalmente de deprecación de
pandas/exchange_calendars; no acreditan un fallo SMTP. Compilación de módulos y
`git diff --check` correctos.

La firma congelada de crecimiento/verificada sigue siendo
`8200fd930b8c8eab529917ea29bd922b32ecc791785f3c61cbe4f699b88f5f11`.

Sin publicación, envíos manuales ni activaciones reales durante esta revisión.
Quedan la comprobación visual del correo en un cliente real, la publicación y
la activación/selección explícita del nuevo experimento con una recogida real.
