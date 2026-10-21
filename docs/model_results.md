# Resultados del modelo: experimento original congelado

El objetivo es medir el sistema existente, no mejorar sus cifras cambiando sus
parámetros. `config.py`, `indicators.py`, `signal_engine.py` y `backtesting.py`
conservan sus reglas originales. El informe guarda la configuración, los hashes
del código y de los datos, el intervalo, la modalidad y los supuestos de costes.
Un cambio metodológico requiere otra versión o temporada; no reescribe el pasado.
También se conservan las versiones de Python, pandas, NumPy, yfinance y calendarios.
El alojamiento y Actions pueden usar runtimes distintos: cada ejecución deja
constancia de ello y del cambio frente al arranque. Un cambio de código o reglas
bloquea la temporada; un replay que modifica un fill previamente firmado también
se rechaza, sin reescribirlo.

## Cuatro modelos y ninguna optimización retrospectiva

Cada compra solicita exactamente **100 USD de exposición bruta**, con fracciones
teóricas. Se conserva la etiqueta exacta `Entrada fuerte`: una nota alta por sí
sola nunca sustituye esa etiqueta.

| Modelo | Entrada | Salida |
|---|---|---|
| A | Cada sesión con una observación fuerte válida | Primera reducción o venta aplicable al lote |
| B | Nueva activación observada de no fuerte a fuerte | Venta aplicable al lote |
| C | La activación de B tiene además nota ≥85 | La misma salida de B |
| D | Las mismas entradas e importes ejecutados de A | Mantener, sin ventas ni stops |

«Diaria» significa una compra por sesión **observada**, no fabricar señales para
los días sin análisis guardado. Se conserva la primera observación por
ticker/sesión/fuente; las revisiones posteriores quedan como evidencia. Una
primera observación ya fuerte no demuestra una activación de B/C: queda censurada
por la izquierda. La información anterior al inicio puede establecer ese estado,
pero no crea compras anteriores al intervalo. En C, alcanzar 85 más tarde mientras
continúa fuerte no inventa otra activación.

Las fuentes técnica y crecimiento/momentum se calculan por separado. No se
fusionan notas ni se rellenan factores históricos con fundamentales actuales.
El universo es el historial observado completo, no sólo las favoritas de hoy;
esto no equivale a un universo bursátil libre de sesgo de supervivencia.

## Salidas propias de cada lote

A incluye `Reducir` **o el estado más severo `Vender`**. El evaluador original da
prioridad a venta; esperar una etiqueta de reducción exclusiva podría omitir una
salida válida. En este experimento reducir cierra el lote entero, no una fracción.
B/C esperan venta. Los tres conservan el stop nativo al cierre del evaluador de
posiciones —8% con la configuración original— desde su propia entrada simulada.
Nunca heredan el precio de compra, la etiqueta o la rentabilidad de la cartera
real del usuario. D no aplica esas salidas.

La decisión del cierre se ejecuta en la primera apertura regular posterior a su
disponibilidad. Un gap se ejecuta al precio de esa apertura, no al stop ideal.
No se añade trailing intradía a A/B/C. La referencia `original` de la auditoría
conserva por separado el stop, trailing y señales del backtest existente; el
backtest original mantiene además su propio asignador de capital y costes por
activo. Ninguna de esas referencias es una quinta cartera A–D comparable sin
explicar sus distintas aportaciones y restricciones.

Los indicadores todavía inmaduros producen `pd.NA` en las reglas de salida, no
`False` ni una venta inventada. Si la trayectoria de un lote depende de una salida
no determinable, permanece sin resolver y no certifica un resultado total.

## Dos modalidades de dinero separadas

- **Teórica:** aportaciones externas ilimitadas de 100 USD por compra, sin costes.
  Antes del primer aporte, efectivo y patrimonio inicial son cero; los 100 USD
  definen cada lote, no un capital inicial ficticio.
  El efectivo de las ventas se conserva separado, sin intereses ni reinversión.
  Nunca desaparece del patrimonio ni financia una compra posterior.
- **Presupuestaria:** un único capital inicial explícito, sin recargas. Las ventas
  netas conocidas pueden financiar nuevas compras. Sólo se admiten lotes enteros
  de 100 USD; si no alcanza para exposición, costes y reserva de comisión de
  salida, se rechaza la compra, sin sobregiro ni compra parcial.

La entrada presupuestaria paga sus costes **además** de los 100 USD brutos; esos
costes no cambian el precio nativo usado por el stop. Los valores iniciales del
simulador —capital 6.000 USD, comisión fija 1 USD por lado, spread adverso 10 pb
por lado y cambio 25 pb por lado no USD— son hipótesis configurables del
experimento, no tarifas verificadas de un broker. El manifiesto conserva los
valores concretos. No se modelan impuestos, dividendos, intereses ni retrasos de
liquidación. La reutilización instantánea de efectivo conocido es un supuesto
de simulación, no una garantía operativa.

D solicita la cohorte de A dentro de cada modalidad. Con presupuesto finito,
mantener puede impedir financiar entradas que A sí paga con ventas. Esas compras
rechazadas se muestran; `cohort_matches_A=false` impide presentar los totales de
A/D como un contrafactual de lotes idénticos. Se conservan también los eventos
comunes para una comparación explícita de esa subcohorte.

## Tres relojes, más la disponibilidad del dato

1. `signal_session` identifica la sesión analizada; no permite actuar ese día
   por sí sola. `observed_at` procede del `created_at` archivado y determina
   cuándo se observó la recomendación. Esa marca es generada por la aplicación,
   no una atestación independiente del servidor.
2. `close_at` es el cierre del calendario. `source_close_available_at` conserva
   cuándo llegó cada barra final; `close_available_at` es el máximo acumulado
   de conocimiento necesario para sus indicadores, incluidos datos previos que
   llegaron tarde. Una decisión no puede adelantarse a ese reloj efectivo.
3. El instante económico de una ejecución simulada y el instante en que se
   conoció su precio se conservan separados. Registrar un fill después no hace
   que su precio estuviera disponible para decidir antes.
4. Los correos tienen eventos propios de planificación, intento, fallo y
   aceptación SMTP. Una señal guardada no prueba que se enviase un correo.

En una reconstrucción histórica puede desconocerse la hora real de llegada de
las barras. En ese caso se usa el cierre del calendario **sólo como proxy
retrospectivo**, con `close_availability_assumed=true`. No se convierte ese
supuesto en evidencia prospectiva. Si una columna de disponibilidad explícita
contiene marcas ausentes, sin zona horaria o anteriores al cierre final, no se
vuelve silenciosamente al proxy: su historia dependiente queda no disponible.

El arranque prospectivo fecha su histórico inicial de calentamiento en el
instante de activación. Los nuevos datos llegan con su instante real de
recogida. No reconstruye supuestas decisiones anteriores al arranque usando una
descarga posterior. Un dato tardío puede originar un nuevo evento o revisión,
pero no modificar un snapshot previamente publicado.

Si falta una intención registrada antes de una ejecución reconstruida, se
bloquean **todas** las nuevas operaciones de esa temporada, no sólo su cifra de
rentabilidad. Una venta que sólo existe en el replay nunca puede financiar otra
compra registrada. Las revisiones del proveedor también bloquean operaciones;
la evidencia anterior permanece y hace falta una temporada independiente.

Una interrupción creando cabeceras se puede reanudar usando su instante y
configuración originales. Las temporadas incompletas no impiden actualizar las
completas del mismo usuario. Los ocho modelos/modos tienen transacciones propias,
no se promete una transacción distribuida que abarque todas sus ejecuciones.

## Precios y divisas

La descarga histórica es una reconstrucción, no una cinta archivada de mercado.
Se declara la base ajustada por splits, sin doblar ajustes de cantidades. GBp/GBX
se convierte una vez a GBP antes de indicadores, stops y FX. Precios incoherentes
o cambios inexplicados del orden de 100× quedan en cuarentena, no se cuentan como
una caída real de inversión.

El calendario acredita festivos, horario de verano y cierres anticipados. Si
falta la apertura que correspondía, no se compra en otra más conveniente. Sólo
se arrastra una valoración cuando el calendario demuestra que el mercado estuvo
cerrado, conservando la fecha real de la marca; un precio ausente no es cero.
La prueba de cierre sólo vale dentro de `calendar_coverage_start/end`, incluido
su rango de fechas sin sesión. Fuera del horizonte publicado no se inventan
festivos ni se certifica una cotización antigua como actual.

El FX es USD por unidad de moneda principal y sólo se utiliza después de estar
disponible. Su límite de antigüedad es **7 días naturales transcurridos** por
defecto, parámetro de calidad de datos separado de `StrategyConfig`. Se conservan
fecha y edad de las cotizaciones antiguas, pero el tipo queda N/D para ejecutar o
valorar. Nunca se rellena con FX futuro. Un movimiento de FX no dispara por sí
solo un stop nativo.

## Métricas y referencias homogéneas

El patrimonio es efectivo más posiciones abiertas. El resultado separa
aportaciones externas, exposición invertida, ventas, costes, P/L realizado y
latente. La rentabilidad neta divide por las aportaciones externas, no por una
mezcla de aportaciones y capital reciclado. Se muestran operaciones cerradas,
acierto, duración, capital máximo simultáneo, beneficio/pérdida, profit factor,
ratio de ganancias/pérdidas, rotación y atribución por ticker cuando se conocen
sus datos. Una división no definida permanece N/D.

TWR y drawdown están etiquetados como **aproximaciones diarias**: suponen flujos
externos al inicio del día y no puentean valoraciones ausentes. La TIR/XIRR no se
publica antes de un año ni con flujos o valoración incompletos; no se anualizan
unas pocas semanas para insinuar una expectativa de beneficio.

SPY, QQQ y VT representan referencias de precios, sin dividendos en efectivo.
Además, cada A/B/C tiene su comparador de mantener sus propios lotes exactos
(identidad, fecha, cantidad, precio, FX y costes de entrada); D ya mantiene los
de A en su modalidad. Si la cohorte completa requiere capital adicional porque
el modelo original reciclaba ventas, esa referencia queda N/D e indica el
déficit, sin préstamos, recargas ni sustituirla por una selección más conveniente.
En modo teórico reciben los mismos importes y fechas de aportación realmente
ejecutados por cada modelo. En modo presupuestario reciben el mismo único
capital inicial y sus costes de compra declarados: las ventas del modelo no se
transforman en aportaciones adicionales al índice. Una referencia incompleta no
repara la cobertura de la estrategia ni se sustituye por cero.

No se proclama un ganador por un historial corto. Sensibilidades, recuperación
posterior y escenarios son diagnósticos ex post o hipótesis, no reglas que
estuvieran disponibles al decidir ni una optimización autorizada del sistema.

## Registro inmutable, privacidad y exportación

El namespace `model-original-v1` separa este experimento de la cartera real y
del laboratorio anterior. Cada propietario, temporada, estrategia y modalidad
queda aislado. Los eventos tienen identificadores semánticos deterministas,
hashes y relojes explícitos; los reintentos idénticos son no-op y los contenidos
discordantes producen conflicto. Las escrituras por lote son transaccionales y
usan la cabeza anterior de la cadena para detectar escritores desactualizados.

Una nueva revisión de un día usa otro `run_id`: se añade, nunca actualiza ni
borra la anterior. El evento conserva identidad global dentro de la temporada,
de modo que revisar un snapshot no vuelve a comprar ni a enviar el mismo hecho.
Los hashes permiten detectar discordancias frente a la evidencia conservada;
no convierten una base de datos administrable en una prueba criptográfica
independiente e imposible de alterar.

La contabilidad autoritativa es USD y usa campos USD. **No se escribe un valor
USD dentro de `initial_nav_eur`, `net_nav_eur` ni otro campo EUR.** Una proyección
opcional a las tablas paper EUR exige importes EUR explícitos y una referencia
observada de FX; nunca se supone EUR/USD=1. Las operaciones y posiciones reales
no se modifican. El registro evita credenciales, destinatarios y direcciones de
correo; los recibos de aceptación se conservan mediante hash.

`smtp_accepted` requiere evidencia de aceptación SMTP, no demuestra entrega al
buzón ni lectura. La ausencia de un registro de aceptación no se presenta como
correo enviado. Una clave de envío idempotente debe comprobarse antes de
reintentar; un resultado ambiguo no autoriza duplicar el correo automáticamente.

El informe retrospectivo y la preparación del Excel no escriben en la base de
datos ni envían correos. Cargar historial, descargar precios, calcular, iniciar
una temporada prospectiva y activar una programación son acciones distintas.
Crear código o una migración no activa por sí solo una tarea ni modifica una
base remota. El Excel conserva resumen, curvas, lotes, ventas, atribución,
referencias, incidencias y manifiesto; los strings no se interpretan como
fórmulas y se ocultan identificadores de cuenta. Las exportaciones y cachés
privadas deben permanecer fuera del repositorio público.
