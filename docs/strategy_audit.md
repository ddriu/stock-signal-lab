# Auditoría de estrategias — fase 1

Esta página mide observaciones históricas; no genera órdenes, no escribe en la
cartera, no cambia las reglas actuales ni envía correo. Está en **Analizar →
Validar método → Auditoría de estrategias**, en la rama de evaluación. No debe
confundirse con el backtest técnico existente ni con el laboratorio de cartera.

## Evidencia y reproducibilidad

- Se carga el historial completo del usuario autenticado, no sólo sus favoritas
  actuales. La consulta es de lectura. Cada canal se estudia por separado:
  técnico (`entry_label/entry_score`) y crecimiento/momentum (política archivada).
- `created_at` determina disponibilidad. Es una marca generada por el cliente,
  no una atestación independiente del servidor. `analyzed_at` identifica la
  sesión analizada, no la fecha en que alguien podía ejecutar la recomendación.
- Dentro de ticker/sesión/canal se conserva la primera observación disponible;
  revisiones posteriores se exportan como evidencia, sin seleccionar la mejor.
  Los identificadores discordantes se rechazan, no se resuelven arbitrariamente.
- Una etiqueta observada intradía es evidencia de una recomendación **parcial**,
  no una señal confirmada al cierre. Se conserva con advertencia y no se vuelve
  a calcular la entrada usando el cierre que sucedió después.
- Una puntuación ≥85 sin la etiqueta exacta `Entrada fuerte` nunca compra.
  `position_label` de una inversión real no se utiliza para cerrar lotes virtuales.
- Sólo se usan factores fundamentales archivados. No se rellenan con los
  fundamentales que el proveedor publica hoy. No hay garantía de disponibilidad
  de las configuraciones históricas: las salidas se reconstruyen con la
  `StrategyConfig` congelada y exportada, no se afirma una reproducción exacta
  de configuraciones antiguas no conservadas.
- Los hashes del historial, configuración, señales y barras permiten identificar
  una ejecución. Para repetirla exactamente, reutilizar la exportación privada
  y la caché: una descarga posterior puede contener revisiones del proveedor.

## Entradas y dinero

La primera prueba es bruta, en USD, fracciones teóricas, sin costes, impuestos,
límites de capital, financiación, intereses sobre efectivo ni reinversión de las
ventas. El dinero recuperado se separa como efectivo; sigue formando parte del
patrimonio del inversor y no desaparece del cálculo de rentabilidad.

| Variante | Compra |
|---|---|
| A1/A2/A3 | 1 USD cada sesión con `Entrada fuerte` |
| B1/B2/B3 | 1 USD sólo al pasar de otro estado observado a `Entrada fuerte` |
| C80/C85/C90 | A con puntuación mínima 80/85/90, manteniendo la etiqueta |
| D fija | 100 USD por señal |
| D escalonada | 100/200/300 USD para 75–79/80–84/≥85 |
| D factores | D escalonada × mínimo de calidad/valoración/oportunidad/riesgo /100 |

La variante de factores es una hipótesis predeclarada, no una regla del radar
original. Factores ausentes o fuera de rango impiden dimensionar ese lote.
Una primera observación ya fuerte no demuestra una nueva activación B; queda
marcada como censurada por la izquierda. Las observaciones anteriores al inicio
del intervalo sirven para establecer el estado previo, nunca para comprar antes.

La ejecución es la **primera apertura regular posterior** a la disponibilidad,
como aproximación conservadora con datos diarios. No es una cinta intradía ni
garantiza ejecución al bid/ask. No se usa el precio de la fotografía. Si falta
la apertura prevista por el calendario, no se salta a otra más conveniente.

## Salidas independientes

- A1/B1/C-reduce: `reduce_setup` o `sell_setup` reconstruidos al cierre; el
  stop propio del 8% al cierre también puede producir `Vender`, como en el
  evaluador de posiciones actual. Orden en la siguiente apertura.
- A2/B2/C-sell: sólo `sell_setup` o stop propio del 8% al cierre. No confundir
  este último con un stop intradía. Orden en la siguiente apertura.
- A3/B3/C-hold: mantener hasta la valoración final, sin ventas.
- Alternativa confirmada: dos cierres consecutivos bajo la media larga.
- Alternativa trailing10: 10% desde el máximo conocido previamente. El máximo
  de hoy sólo modifica el stop de sesiones posteriores; no inventa el orden de
  los movimientos intradía. Alternativa fixed8: stop intradía del 8%.
- `original`: política del backtest existente: stop y trailing configurados,
  y salidas técnicas/por reducción según la configuración congelada.
  Replica las salidas, no su asignador de capital ni límite de posiciones.

Cada lote mantiene entrada, cantidad, máximo y stop propios. Un gap ejecuta al
precio de apertura peor, no al stop ideal. Con OHLC no puede conocerse el instante
de un stop intradía: se identifica así y se usa el FX de apertura como **proxy**,
no el tipo futuro del cierre. Si faltan datos que impiden determinar una salida,
la trayectoria queda sin resolver, no se convierte en mantener ganador.

## Precios, calendarios y divisas

Yahoo/yfinance aporta OHLC diarios reconstruidos con `auto_adjust=False`,
`repair=False`; su base ya ajustada por splits se declara explícitamente.
No se mezclan barras con distintas bases ni se ajustan cantidades dos veces.
Los dividendos en efectivo **no están incluidos**, ni en acciones ni en ETF:
la comparación es de precios, no de rentabilidad total neta.

Los calendarios de `exchange_calendars` incluyen festivos, cambios de horario y
cierres anticipados. IOB utiliza el calendario regular de Londres; los mercados
sin correspondencia verificada permanecen sin cobertura. Una apertura diaria
no modela subastas, liquidez escasa o sesiones extendidas.

GBp/GBX se convierten una vez a GBP **antes** de indicadores y stops. El efectivo
en USD se convierte a moneda principal con FX disponible antes de la apertura;
los cierres diarios FX se consideran disponibles conservadoramente a las 02:00
UTC del día siguiente. No hay backfill del FX futuro. La moneda del listado
se obtiene al descargar: su continuidad histórica es un supuesto declarado.

OHLC inconsistente, precios no finitos, splits sin base coherente o saltos de
unidad del orden de 100× ponen en cuarentena esa sesión y su tramo posterior.
No borran el pasado válido. Una cotización ausente no es un precio cero ni una
caída del 99%. Sólo se conserva un cierre anterior si el calendario acredita
que no ha habido otra sesión esperada: un festivo no equivale a dato perdido.

## Métricas, referencias y recuperación

El ROI es `(patrimonio final − aportado) / aportado`. Se separan aportaciones,
importe recuperado, efectivo, valoración abierta, resultado realizado y latente.
No se muestra un ROI global de cobertura completa cuando hay compras imposibles
o posiciones sin valorar. Puede haber un subtotal verificable, expresamente
distinto de la estrategia completa.

El TWR exacto y el drawdown exacto quedan sin calcular sin marcas intradía en
cada flujo. El TWR/drawdown **aproximados diarios** suponen aportaciones al inicio
del día y están nombrados como tales. La XIRR anualizada queda pendiente en esta
fase; un historial de pocas semanas no justifica extrapolar ganancias anuales.

SPY, QQQ y VT representan S&P 500, Nasdaq 100 y mercado mundial. Cada referencia
recibe los mismos importes de los lotes ejecutados, desde su primera apertura
prevista por calendario no anterior al flujo; si falta, queda pendiente.
No se compara una inversión inicial única con
aportaciones sucesivas. Una contribución sin ejecución o cierre final invalida
el total de esa referencia, sin esconderla.

El contrafactual mantener conserva los **mismos lotes e importes** de la variante
evaluada. Las diferencias entre venta y mantener al final, recuperación al coste
en USD y sesiones hasta equilibrio son diagnósticos **ex post**, nunca reglas
que se supiera aplicar antes. Los escenarios +5/+10/+15/+20 son hipótesis con FX
constante, no pronósticos. Las ventanas de corrección son descriptivas y las
fechas las fija el usuario; el historial completo permanece siempre principal.

## Uso reproducible de la CLI

Guardar las páginas JSON de snapshots fuera del repositorio público. La CLI
rechaza cachés y resultados dentro de éste. No subir ficheros financieros,
credenciales ni exportaciones de usuarios a GitHub.

```sh
python -m scripts.run_strategy_audit \
  --snapshots /ruta/privada/snapshots \
  --cache /ruta/privada/cotizaciones \
  --output /ruta/privada/resultados \
  --source technical --start 2026-09-28 --end 2026-10-19 --download
```

Repetir sin `--download` reutiliza los precios congelados. Se exportan las 28
variantes predeclaradas, operaciones, lotes, evolución, incidencias, sectores,
contrafactuales, índices, seguimiento y manifiesto de procedencia. Crecimiento
requiere una ejecución separada `--source growth`; nunca se mezclan sus notas.

## Qué no está validado todavía

1. Esta fase entrega motor, validación sintética, CLI y panel independiente.
   No cambia entradas/salidas del radar para mejorar retrospectivamente cifras.
2. Las fotografías históricas no prueban qué correo recibió el usuario. El estado
   mutable de notificación sólo conserva el último envío; falta un registro
   histórico de cuerpo, mensaje, resultado y hora de cada entrega. No se inventa.
3. El seguimiento a 5/20/60/120 sesiones es de lectura y deja horizontes futuros
   pendientes. **El registro automático prospectivo e inmutable de cambios y
   entregas de correo requiere una segunda fase**, nueva tabla/versionado y
   activación separada. Esta evaluación no envía correos ni modifica datos.
4. El análisis de sensibilidad con omisión manual de compras de una fecha y la
   XIRR quedan pendientes. Nunca se eliminan retornos negativos del resultado.
5. Los umbrales/salidas alternativos son hipótesis, no una optimización terminada.
   Antes de usar dinero real falta un protocolo fuera de muestra congelado,
   suficientes recomendaciones futuras y modelar costes/ejecución reales.
   Agosto tiene cobertura dispersa y octubre pocas sesiones; no se selecciona
   ganador ni se presenta significación estadística no demostrada.

Fuentes técnicas: [yfinance PriceHistory](https://ranaroussi.github.io/yfinance/reference/yfinance.price_history.html),
[calendario de mercados](https://github.com/gerrymanoim/exchange_calendars/blob/master/exchange_calendars/exchange_calendar.py),
[horario IOB de LSE](https://docs.londonstockexchange.com/sites/default/files/documents/LSE_INTERNATIONAL_ORDER_BOOK_FACTSHEET_OCT_2020.pdf).
