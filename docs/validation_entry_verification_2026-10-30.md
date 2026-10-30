# Corrección de procedencia y lectura de entradas · 30/10/2026

## Alcance

Corrección de datos y presentación; no optimización de estrategias. Se conservan
`StrategyConfig`, crecimiento/momentum, indicadores, puntuaciones, umbrales,
stops, reglas de salida y motores de simulación. La revisión se ha realizado
localmente, sin operaciones reales, escrituras en la base de datos ni envíos SMTP.

## Fallos corregidos

1. La recuperación Yahoo sólo intentaba cuentas anuales cuando faltaban cifras,
   no cuando existían ratios pero faltaban sus periodos. Ahora recupera valores
   recalculados y su propia procedencia, manteniendo prioridad SEC.
2. El resumen ocultaba señales fuertes bajo «Revisar datos». App y correo
   distinguen señal del motor, suficiencia de datos y siguiente paso. Las señales
   fuertes pendientes no se anuncian como compras verificadas.
3. La vista de crecimiento podía preparar un plan sin pasar el mismo control
   empresarial del resumen. Ahora conserva la señal original, pero no habilita
   precio/stop/tamaño mientras falte esa verificación.
4. El detalle de una favorita podía utilizar la primera fila de la cartera en
   vez de la empresa seleccionada. Ahora selecciona por ticker.
5. El respaldo no inventa FCF sin capex, no certifica ROE entre ejercicios no
   consecutivos, ni ratios con periodos/unidades incompatibles. SEC conserva la
   fecha de publicación de todas las piezas, incluidas comparativas restatadas.
6. Se reconoce el concepto general IFRS15 `RevenueFromContractsWithCustomers`.
   En TORM, el concepto antiguo `Revenue` sólo ofrecía 2015: no se mezcla ya con
   resultados operativos de 2025 cuando existe el concepto actual. No se suman
   subcomponentes de ingresos ni se modifica la selección general de conceptos.
7. La caché incorpora explícitamente `dated_statement_fallback_v2`; una revisión
   manual actualiza fundamentales junto con precios. Se comprueba la importación
   tras un despliegue en caliente.
8. Se muestran monedas contables, no monedas de cotización, para cifras de caja.
   Los números antiguos guardados como texto no rompen el correo ni la tabla.
9. Se recargan de forma ordenada las dependencias incompatibles en sesiones ya
   abiertas, antes de enlazar las nuevas funciones del resumen. Una sesión con
   módulos actuales no se recarga ni pierde su estado. La regresión cubre módulos
   antiguos de datos, validación, digest y referencias enlazadas a funciones viejas.

## Presentación

Cartera primero; después fuertes con datos suficientes, fuertes pendientes y
candidatas. El resto de favoritas queda en un desplegable. El histórico de cinco
sesiones y la procedencia por métrica se consultan en el detalle. No se rellenan
notas ausentes ni se convierte la falta de datos en una venta.

El correo incluye los contadores de señales verificadas/pendientes y hasta tres
fuertes pendientes, aunque los avisos de posiciones ocupen el resumen. Usa el
mismo informe versionado que la aplicación. Los informes archivados mantienen
sus datos y su SHA-256; no se reescriben con los nuevos fundamentales.

## Comprobación con proveedores reales

Consultas de sólo lectura el 30/10/2026, evaluadas con esa fecha, sin descargar
carteras, modificar preferencias ni ejecutar el runner de correo:

| Empresa | Procedencia de las cinco métricas empresariales | Periodo | Resultado del control de datos |
| --- | --- | --- | --- |
| Microsoft | SEC EDGAR | Anual, cierre 30/06/2026 | Cinco métricas fechadas; sin bloqueo por datos |
| Edenred | Yahoo Financial Statements | Anual, cierre 31/12/2025 | Cinco métricas recuperadas con periodo; sin bloqueo por datos |
| TORM | SEC EDGAR y FCF de Yahoo Financial Statements | Anual, cierre 31/12/2025 | Cinco métricas fechadas tras reconocer IFRS15; sin bloqueo por datos |

Esto valida la recuperación y sus controles, **no constituye una señal de
compra** ni garantiza que las otras empresas tengan datos suficientes. Las
cuentas anuales no se presentan como el último trimestre. Si Yahoo no acredita
fecha de publicación, queda no documentada; estas consultas no se convierten en
señales que supuestamente fueran conocidas antes de su registro.

## Pruebas y límites

Suite completa: **1.349 pruebas aprobadas**, con 1.147 avisos, principalmente de
deprecación de dependencias. Comando: `python -m pytest -q --disable-warnings`.
Protección adicional de despliegue en caliente: **32 pruebas focales aprobadas**,
incluidos cinco escenarios nuevos en subprocesos aislados. La publicación queda
sujeta además a las pruebas completas y de migraciones de GitHub Actions.

Se verifican recuperación parcial, prioridad SEC, asociación valor-periodo,
divisas, huecos de años, duplicados contradictorios, FCF sin capex, ETF, importación
en caliente, caché versionada, ausencia de efectos externos al leer informes,
consistencia app/correo, datos legacy y selección de una empresa con otra posición
invalidada en cartera. `git diff --check` y la comparación de los archivos del
motor congelado no presentan cambios.

La publicación y la generación de una revisión nueva son pasos posteriores.
No se asegura cotización en tiempo real, disponibilidad universal de datos,
rentabilidad futura ni entrega de correos a partir de estas pruebas.
