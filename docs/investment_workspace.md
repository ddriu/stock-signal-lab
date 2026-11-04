# Decidir por plazo, medir por separado

Esta reconstrucción simplifica la presentación; no sustituye ni optimiza el
motor original. Revisión diaria significa revisar operaciones de varios días,
no invertir dentro de la misma sesión con cotizaciones gratuitas de cierre.

## Navegación

| Pantalla | Para qué sirve | Qué no hace al abrir |
| --- | --- | --- |
| Inicio | Tres prioridades y accesos por plazo | No recalcula el mercado ni concilia las dos cuentas |
| Invertir | Un plazo: días, semanas o largo plazo; cartera antes que entradas | No combina puntuaciones ni ejecuta órdenes |
| Cartera | Posiciones, movimientos y plan manual por posición | Guardar un plan no cambia cantidades |
| Favoritos | Gestionar listas | No recalcula análisis |
| Resultados | Separar resultado real de simulaciones y consultar evidencia guardada | No activa temporadas ni simula compras atrasadas |
| Herramientas | Análisis detallado y panel anterior, conservados | No se eliminan estrategias o historiales |
| Ajustes | Preferencias de correo y guía | No se cambian destinatarios al navegar |

Los enlaces `?digest=<id>` siguen abriendo exactamente su informe inmutable.
Inicio e Invertir usan el mismo informe versionado que el correo, no un nuevo
cálculo que pudiera contradecirlo. Invertir enlaza contexto exclusivamente por
identidad de snapshot, empresa, sesión y momento de registro. Una revisión
posterior no reescribe lo que se sabía en el informe anterior.

## Reglas de lectura

- Días: configuración técnica observada en cierre; no señal intradía ni precio
  de ejecución asegurado.
- Semanas: crecimiento y momentum originales, sin promediar calidad o notas
  técnicas auxiliares.
- Largo plazo: tesis, calidad, valoración y exposición. Una entrada técnica
  mala no implica vender una inversión a largo plazo.
- Cada posición necesita un plan explícito; no se deduce por empresa, sector,
  pérdidas o puntuación. Los planes distintos de un mismo ticker requieren
  revisión, no una asignación automática.
- N/D significa evidencia insuficiente. Un cero sólo representa un recuento
  observado, no una descarga fallida. Un score no es un porcentaje de beneficio.
- «Estudiar entrada» es una candidatura, no una orden ni garantía de ganancia.
  Antes de operar hay que revisar precio, costes, divisa, tamaño, concentración,
  próximo evento y condición de salida.

Una orden stop no garantiza el precio de salida; una stop-limit puede no
ejecutarse. Véase [Investor.gov: tipos de órdenes](https://www.investor.gov/introduction-investing/investing-basics/how-stock-markets-work/types-orders).

## Rendimiento y privacidad

Lecturas de sesión cacheadas durante 60 segundos, separadas por propietario.
El informe más reciente es una consulta limitada a una fila. El contexto de
Invertir se limita a cinco fechas ISO y 1000 observaciones; si falta alguna,
queda pendiente, sin descargar todo el histórico ni ejecutar el motor. Los
proveedores sólo se consultan al solicitar una revisión explícita. Navegar no
envía correo ni modifica posiciones.

Los resultados históricos sólo justifican conclusiones durante fechas con
evidencia suficiente. No se atribuyen resultados desde el inicio de una cuenta
si faltan señales o movimientos de ese periodo. El panel avanzado conserva
retrospectiva, seguimiento congelado, flujos, costes y comparadores sin elegir
al ganador a posteriori.

La mejora visual no demuestra una estrategia rentable. La referencia original
y sus parámetros se mantienen para medir si añadir complejidad realmente aporta
algo. No se publican posiciones, importes ni informes privados en este documento.
