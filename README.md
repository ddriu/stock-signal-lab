# Stock Signal Lab

Aplicación local de análisis técnico, señales explicables, backtesting y diario de operaciones.
No envía órdenes a ningún broker y sus resultados no constituyen asesoramiento financiero.

## Puesta en marcha

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

La instalación local solicita las credenciales guardadas en
`.streamlit/secrets.toml`. Ese archivo está excluido de Git y nunca debe subirse.

## Arquitectura

```text
.
├── app.py                    # Composición de la interfaz Streamlit
├── config.py                 # Configuración tipada de estrategia y simulación
├── data/                     # Base SQLite local (creada al usar el diario)
├── src/
│   ├── alerts.py             # Preferencias, cambios de estado y resumen explicable
│   ├── alert_runner.py       # Revisión diaria de favoritos y posiciones
│   ├── email_sender.py       # Envío SMTP intercambiable (Gmail/Resend)
│   ├── data_loader.py        # yfinance, validación y normalización OHLCV
│   ├── data_sources.py       # SEC EDGAR, BCE, Alpha Vantage y trazabilidad
│   ├── auth.py               # Cuentas, roles y contraseñas con hash PBKDF2
│   ├── dashboard.py          # KPIs y valoración común de carteras
│   ├── indicators.py         # SMA, RSI, MACD, volumen y ATR
│   ├── signal_engine.py      # Momento de entrada y gestión de posición
│   ├── fundamentals.py       # Calidad empresarial con cobertura de datos
│   ├── opportunity.py        # Valoración, fortaleza relativa, riesgo y score conjunto
│   ├── entry_opportunity.py  # Timing, eventos, zonas de entrada y concentración
│   ├── growth_momentum.py    # Estrategia mensual dinámica y perfiles sectoriales
│   ├── staircase_projection.py # Aportaciones, escalera y simulación a diez años
│   ├── sector_comparison.py  # Comparación normalizada y liderazgo entre pares
│   ├── backtesting.py        # Simulador long-only sin anticipación
│   ├── visualization.py      # Gráficos Plotly
│   ├── ui.py                 # Diseño responsive y perfiles de configuración
│   ├── risk.py               # Tamaño orientativo de posición y riesgo monetario
│   ├── portfolio.py          # Valoración y comparación de cambios
│   ├── portfolio_rotation.py # Colores, horizontes y filtros prudentes de rotación
│   ├── portfolio_history.py  # Evolución diaria y resumen anual de cartera
│   ├── portfolio_export.py   # Libro Excel con tablas y gráficos editables
│   ├── portfolio_snapshot_import.py # Fotografías XLSX sin inventar operaciones
│   ├── portfolio_snapshot.py # Fotografías y reconciliación por cuenta y ticker
│   ├── market_data_quality.py # Vigencia común de cotizaciones y cobertura
│   ├── navigation.py         # Estado seguro entre favoritos y análisis directo
│   ├── segofactoring_import.py # Importación idempotente del resumen XLSX
│   ├── recommendations.py    # Entradas, retornos históricos y ventas parciales
│   ├── return_calibration.py # Frecuencias históricas por bloques temporales
│   └── journal.py            # Diario SQLite y exportación mediante la UI
└── tests/                    # Pruebas unitarias sin red
```

## Cartera conciliada e historial privado

En **Carteras → Privada → Importar cartera conciliada e historial** se puede
previsualizar un libro compatible antes de confirmar su guardado. La hoja
`Cartera` es una fotografía histórica, no una compra. Las hojas de operaciones
aportan el diario; los resúmenes, referencias y controles FIFO no vuelven a
registrarse como operaciones. Los movimientos posteriores actualizan las
cantidades por plataforma sin sumar de nuevo el saldo inicial.

El guardado es aditivo, transaccional e idempotente. Conserva una copia privada
de los registros anteriores y deja los conflictos pendientes, sin sustituir
operaciones manuales. Los archivos originales, bases privadas y copias de
seguridad nunca deben añadirse al repositorio. La copia está en el almacenamiento
privado del usuario; su lectura no forma parte de la carga habitual de Inicio.

Para una instalación existente de Supabase, aplicar primero
`supabase/migrations/20261015192038_reconciled_portfolio_import.sql`.
El RPC sólo está disponible para el servidor con `service_role`; no se expone a
roles anónimos o autenticados. Una instalación nueva puede usar `supabase/schema.sql`.
SQLite prepara su esquema local al abrir el diario.

**Inicio → Resumen** muestra patrimonio, efectivo, ilíquidos, resultados,
distribuciones y fechas, con detalle mensual/anual y FIFO. Las cifras sin
cobertura suficiente quedan `N/D`, con sus subtotales documentados. El cambio
actual sólo valora las posiciones: no reconstruye liquidaciones históricas.
Sin aportaciones y valoraciones completas no se publica una rentabilidad global
ni se dibuja una curva inventada a partir de compras y ventas.

**Inicio → Decisiones** separa la revisión de la posición de la candidatura de
entrada. Las propuestas de peso y capacidad son límites de concentración, no
dinero disponible ni órdenes. Los enfoques de crecimiento/momentum y comparación
de estrategias permanecen independientes; no se fusionan notas.

Las pruebas de importación usan exclusivamente datos sintéticos. El validador
`node scripts/validate_migrations.mjs /ruta/al/repositorio` incluye el RPC
conciliado sobre PostgreSQL en memoria y comprueba permisos, conflictos,
idempotencia y rollback sin acceder a una base externa.

## Reglas y score

El radar conserva separadas cinco puntuaciones y añade un resumen:

- **Calidad de empresa:** rentabilidad, crecimiento, deuda y generación de caja.
  Se normaliza únicamente sobre métricas disponibles y muestra `N/D` si la cobertura
  es inferior al 40%.
- **Momento de entrada:** tendencia, impulso, MACD, máximos, volumen y distancia a
  la media. Desde 55 es vigilancia, desde 65 entrada interesante y desde 75 entrada fuerte.
- **Valoración:** PER, PEG, precio/valor contable y generación de caja frente al precio.
- **Fortaleza relativa:** comportamiento frente al índice general y, para títulos
  estadounidenses, frente a un ETF sectorial.
- **Riesgo controlado:** volatilidad, drawdown, amplitud diaria y liquidez aproximada.
- **Oportunidad conjunta:** 30% calidad, 15% valoración, 25% momento, 15% fortaleza
  relativa y 15% riesgo. La nota se normaliza sobre componentes disponibles y siempre
  muestra una confianza de datos separada.

MACD suma siempre que está por encima de su señal y recibe una bonificación por cruce
reciente. El volumen suma parcialmente desde 0,8 veces su media y obtiene otra
bonificación desde 1,2 veces. Las lecturas `Mantener`, `Reducir` y `Vender` quedan
separadas para posiciones ya existentes.
Un stop loss sólo puede evaluarse con un precio de entrada; en el backtest se controla
para cada posición simulada y, en el análisis actual, puede indicarse un precio de entrada
opcional para activar esa comprobación.

La interfaz incluye una lectura en lenguaje sencillo de cada señal y traduce el riesgo
elegido a importe de posición, unidades, salida si falla y pérdida monetaria aproximada.
La referencia 2:1 es sólo una relación matemática entre beneficio potencial y riesgo,
no un precio objetivo ni una predicción.

## Interfaz y dispositivos

La portada reúne primero el valor de la cartera y un mapa de la parte cotizada. Cada
posición muestra su peso dentro de esa parte —no sobre el patrimonio ilíquido— y un
estado con texto y color: azul puede recibir capital, verde mantener, amarillo esperar,
naranja revisar peso o rotación, rojo revisar una posible salida y gris actualizar datos.
Rojo y azul nunca son órdenes automáticas. Las vistas diaria, semanal, mensual y anual
cambian los pesos del análisis y la cadencia: la diaria sólo vigila, mientras la mensual
es la frecuencia preferente para estudiar cambios. Un bloque de máximo cinco prioridades
resume primero las revisiones y alternativas; el gráfico y las tablas quedan como detalle.
La ruptura de tesis puede declararse manualmente con un motivo y se conserva en la
fotografía de cartera. Ese estado habilita una revisión roja, no una venta automática.
La navegación separa inicio, análisis,
favoritos, carteras y guía para cargar únicamente la sección que se está consultando.
Las oportunidades se presentan en tarjetas fáciles de leer y conservan el ranking
completo y los gráficos técnicos como detalle desplegable.
La pestaña independiente **Oportunidades** mantiene el Radar original para poder
compararlos. Distingue score técnico de timing actual, detecta movimientos ya extendidos
y resultados próximos, calcula zonas desde ATR, medias, soportes y ruptura, y muestra
invalidación, beneficio/riesgo y concentración sectorial. La nota final redistribuye de
forma explícita los pesos cuando faltan fundamentales y conserva la cobertura de datos.
Dentro de **Analizar** hay además un desplegable de favoritas y un buscador con
autocompletado sobre favoritas, análisis guardados y empresas abiertas recientemente.
En **Favoritos**, la tabla permite analizar, editar etiquetas o quitar una empresa
desde su propia fila; ya no existe un segundo selector separado al final de la lista.
La consulta diaria y el formulario para añadir empresas aparecen como dos vistas separadas.
Todas las secciones principales conservan una franja de subnavegación a la misma altura;
Inicio separa resumen, cartera y alertas para evitar una portada interminable.

El diseño se adapta a escritorio, tablet y móvil: las columnas se apilan, la navegación
permanece accesible y las tablas y pestañas pueden desplazarse horizontalmente. La barra
lateral con periodos, estrategia y riesgo sólo aparece dentro de **Analizar**; el resto
de secciones conserva un buscador compacto para no ocupar espacio innecesario.
La configuración principal se presenta en tres pasos —empresas, lectura y ajustes
opcionales— y las herramientas que no necesitan precios, como la proyección de capital,
ocultan la barra técnica. Al abrir una empresa o guardarla desde el buscador rápido, el
panel se cierra automáticamente y deja visible la pantalla de destino.
Los gráficos dividen los títulos largos, calculan automáticamente los márgenes y parten
en varias líneas los nombres extensos de activos para que no desaparezcan en móvil.

Los perfiles **Equilibrado**, **Crecimiento** y **Prudente** ajustan juntos los umbrales
técnicos y de riesgo. **Personalizado** conserva los valores elegidos manualmente.
Cambiar de perfil facilita la configuración, pero no modifica la calidad de los datos
ni convierte una señal retrospectiva en una predicción.

La página independiente **Crecimiento y momentum** no es otro preset del score principal.
Reserva un porcentaje configurable de la aportación mensual, limita el bloque dinámico
sobre la cartera líquida y mantiene separadas tres notas: crecimiento empresarial,
momentum y contexto de mercado/riesgo. El tamaño se limita simultáneamente por presupuesto
mensual, riesgo monetario, máximo individual y techo total de estrategia. Los perfiles de
tecnología, consumo, energía/uranio, biotecnología, industria/defensa, finanzas y ETF
adaptan el riesgo y la lista de comprobaciones; el usuario puede corregir manualmente una
clasificación automática. Segofactoring y Civislend quedan fuera de estos cálculos.
Los registros de Civislend y Segofactoring no se borran: permanecen en una vista secundaria
para cuadrar patrimonio y vencimientos. Quedan fuera de la portada líquida, colores,
benchmark y candidatos de rotación porque no son comparables ni tienen la misma liquidez.
La proyección histórica puede conservarlos como bolsas separadas, sin mezclarlos con la
selección de empresas. La escalera sólo aumenta su porcentaje después de
un año que supera el umbral elegido. Muestra capital aportado, cuatro escenarios,
percentiles de 1.000 recorridos y los horizontes diciembre, 12, 24, 36, 48 meses y
10 años; no modifica operaciones ni presenta la simulación como una garantía.
En el radar, los resúmenes de empresas revisadas, entradas para validar, vigilancia y
datos pendientes son accesos pulsables. Cada grupo permite seleccionar un ticker para
preparar su plan dinámico o abrir directamente su análisis completo.
Desde esta página se puede revisar toda la lista privada o añadir también la lista del
grupo. El barrido conserva bloques ya descargados, admite hasta 200 tickers por pasada y
completa los datos empresariales en grupos de 25 para no bloquear el alojamiento gratuito.
Una empresa que sólo tenga precio y momentum aparece como `Pendiente de fundamentales`:
nunca se presenta como entrada completa hasta disponer de la revisión empresarial.

## Cartera y cambios

El diario guarda compras y ventas con fecha, cantidad, precio, moneda y comisión. Reconstruye
los costes por lotes FIFO incluyendo las comisiones, admite ventas parciales y calcula beneficio realizado
y beneficio neto si se vendiera al último cierre. Las posiciones guardadas se añaden
automáticamente a la próxima descarga.

La pestaña **Resultado real aproximado** reconstruye las operaciones por lotes FIFO y resume
compras, ventas, beneficio bruto, comisiones, costes de cambio, impuestos estimados y resultado
neto por año. Prioriza el importe realmente liquidado por el bróker cuando está disponible y,
si falta, permite configurar de forma visible comisión, spread, coste de divisa y tipo fiscal.
Es una ayuda para decidir y comprobar órdenes, no una liquidación tributaria oficial.
Si falta el precio, la divisa o una operación necesaria, se muestran los importes que sí
pueden comprobarse pero se oculta el porcentaje agregado. Ese porcentaje combina ventas
del año elegido y posiciones abiertas actuales; no se presenta como TWR, XIRR o rentabilidad
anual de toda la cartera.
La portada aplica la misma cautela: no suma un resultado parcial como si fuera el total
cuando alguna inversión de la fotografía carece de coste o ganancia/pérdida comprobable.

El mapa de rotación compara una posición sólo con empresas guardadas explícitamente en
favoritos. Antes de mostrar un cambio exige que la posición origen ya necesite revisión,
que la alternativa tenga entrada y confianza suficientes, que no empeore materialmente
calidad o riesgo y que respete concentración sectorial. También separa comisión, spread,
divisa, reserva fiscal, capital prudentemente reinvertible, correlación y el porcentaje que
la alternativa tendría que recuperar. Si falta el coste FIFO, no propone el salto.

La pestaña **Favoritos** permite buscar una empresa por su nombre normal, sin conocer el
ticker, y guardarla en una lista privada o compartida. Los resultados indican mercado,
país, moneda y tipo de cotización para distinguir una acción local de alternativas ADR,
GDR u OTC. Admite mercados internacionales como Madrid (`.MC`), Tokio (`.T`), Londres
(`.L`) o el mercado internacional de Londres (`.IL`). Cada lista admite 300 empresas.
La vista principal es una tabla densa, no una colección de tarjetas: cada fila conserva
ticker, nombre, mercado y etiquetas. Al seleccionar una fila aparece el acceso al análisis
completo. Se muestran hasta 25 empresas por página, con búsqueda y paginación para que las
listas largas sigan siendo manejables desde móvil.
Se pueden escoger hasta 25 a la vez para descargar fundamentales y hacer el análisis completo.
Si el buscador externo no responde, el modo avanzado permite guardar el ticker exacto
manualmente. Nintendo y Kazatomprom conservan además cotizaciones conocidas como respaldo
del buscador (`7974.T`, `NTDOY` y `KAP.IL`).
Cada favorita admite hasta cinco etiquetas visuales —por ejemplo Energía,
Biotecnología, Tecnología, ETF, Fondo o Small cap— que pueden corregirse y utilizarse
como filtro. La aplicación propone etiquetas a partir del tipo de instrumento, sector,
industria y capitalización disponibles, pero la clasificación sigue siendo editable.
Inicio prioriza los precios de las posiciones reales y las del laboratorio en lotes
de 20 al cargar la aplicación. No descarga todas las favoritas por abrir el resumen:
el barrido completo se solicita desde **Decisiones → Revisar toda mi cartera y buscar
oportunidades** o desde el radar, y el proceso de correo tiene su revisión diaria
independiente. En la misma sesión se conservan las descargas recientes correctas y
se reintentan las pendientes; una cotización antigua no cuenta como actualizada.
La revisión empresarial profunda continúa en lotes separados para no confundir un
precio reciente con fundamentales completos. El historial de operaciones no tiene
ese límite.

Inicio separa cuatro vistas y sólo ejecuta la seleccionada: **Resumen** muestra
saldos y cobertura; **Decisiones** presenta riesgos y alternativas; **Laboratorio**
registra y compara simulaciones; **Accesos** abre las tareas habituales. Civislend,
Segofactoring y otras partidas manuales conservan su valor declarado, pero no se
mezclan con las propuestas de rotación de valores cotizados.

El laboratorio no registra la barra de la sesión actual de SPY hasta las 16:30 de
Nueva York, con ajuste de horario de verano y un margen prudente tras el cierre.
Antes del umbral conserva el histórico y no inicia ni reconstruye temporadas.
También rechaza fechas futuras o de fin de semana. Esta guardia no sustituye un
calendario completo ni garantiza que el proveedor haya finalizado la cotización.

La sección **Comparador sectorial** enfrenta de 2 a 10 empresas cargadas durante
1, 3, 6 o 12 meses. Normaliza todas las cotizaciones a 100, calcula rentabilidad,
volatilidad, drawdown, distancia al máximo y correlaciones, y asigna un liderazgo
relativo únicamente dentro de la selección. Calidad, valoración y riesgo continúan
separados para evitar que una subida reciente se interprete como calidad empresarial.

La sección **Objetivo 30+ días** estudia cada nueva señal de entrada sin convertirla
en una operación rápida: compra de forma simulada en la apertura siguiente y mantiene
la posición durante 21, 42, 63, 126 o 252 sesiones. Descuenta 1 euro al comprar, otro
al vender y el deslizamiento configurado. Convierte las rentabilidades anuales elegidas
para Segofactoring y Civislend al mismo horizonte y muestra por separado la frecuencia
de acabar en positivo, superar Segofactoring y superar Civislend. La evidencia sólo se
marca como suficiente desde 30 bloques temporales no solapados; varias empresas en
la misma fecha no cuentan como periodos independientes. La banda orientativa del
95% se calcula por bloques, no por número de tickers. Es una frecuencia descriptiva,
no una probabilidad predictiva validada fuera de muestra. La calibración utiliza el
score técnico histórico; no aplica fundamentales actuales a fechas pasadas porque
eso introduciría *look-ahead bias*.

Desde la ficha de una empresa se puede guardar una fotografía privada del análisis:
precio, fecha, seis notas, lecturas, expectativa histórica y una nota personal. El
historial permite ver la evolución, abrir de nuevo la empresa y borrar revisiones.
No duplica las series de precios ni las gráficas en la base de datos.
Una oportunidad también puede guardarse desde su ficha expandible. El timing y la zona
quedan en la nota del historial; cuando el ticker está cargado, el historial calcula los
retornos posteriores a 1, 5 y 20 sesiones para empezar a contrastar los distintos rangos
de score sin llamarlos probabilidades.

Cada cuenta mantiene una **cartera privada** separada. Además existe una **cartera del
grupo**, visible únicamente después de iniciar sesión, en la que los cuatro miembros
pueden registrar las decisiones comunes. Cada movimiento compartido identifica quién
lo añadió; un usuario puede eliminar los suyos y el administrador puede corregir cualquiera.
Ambas carteras muestran capital pendiente, valor neto, beneficio latente y realizado,
rentabilidad, comisiones y cobertura de precios. El rol administrador también ve un
resumen de las cuatro carteras privadas y puede registrar operaciones para un usuario.
Cada operación puede asociarse a Trade Republic, Revolut, MyInvestor u otra cuenta. Si
la moneda no es EUR, el campo opcional **Importe liquidado en EUR** conserva el cargo
real de una compra o el abono neto de una venta; así el coste histórico no cambia cuando
varía el EUR/USD. Si ese dato falta, la interfaz identifica el resultado como una
estimación con el cambio actual en lugar de presentarlo como un cuadre exacto del bróker.

La pestaña **Evolución por años** separa compras, ventas, dinero neto aportado, valor de
mercado y resultado acumulado. Incluye una gráfica temporal, otra anual y un Excel con
hojas de resumen, operaciones, posiciones y evolución diaria. Para años pasados usa los
cierres disponibles y el tipo de cambio actual del BCE, por lo que es una herramienta de
seguimiento y no una contabilidad fiscal exacta. En la cartera privada de `ddriu` aparece
además **Mis cuentas**, una vista conjunta y editable de **MyInvestor, Trade Republic,
Revolut, Segofactoring y Civislend**. Las cinco se crean con valor cero y el estado
«Pendiente de actualizar», para poder completar importes sin inventar datos. En Civislend
y Segofactoring el detalle por proyectos registrado manualmente tiene prioridad sobre el
total provisional, porque esos activos no tienen una cotización pública automática. El
Excel incorpora también una hoja independiente con las cuentas y plataformas.

En **Mis cuentas** se puede subir el resumen `.xlsx` de Segofactoring. La vista previa
separa capital pendiente, operaciones cobradas y ganancia neta registrada. Si el mismo
archivo se vuelve a subir, la aplicación actualiza las filas importadas sin duplicarlas;
las participaciones repetidas del documento se conservan como inversiones independientes
y los proyectos añadidos manualmente no se borran. Una operación cobrada queda en el
histórico, pero deja de contarse como capital todavía invertido. Cuando el fichero sea
antiguo puede marcarse como pendiente de actualizar para no presentar sus cifras como actuales.

También se puede importar una **fotografía completa de cartera** en `.xlsx`. Las posiciones
se guardan en una tabla histórica separada del diario de compraventas: una valoración del
bróker no se convierte en una compra si faltan la cantidad, la fecha o el precio ejecutado.
La misma fecha se actualiza de forma idempotente y una fecha distinta añade un nuevo punto
al gráfico de evolución. El panel conserva valor, coste y resultado estimados, muestra la
distribución por plataforma y permite abrir directamente los tickers reconocidos en el análisis.
Segofactoring se separa de MyInvestor al sumar cuentas para evitar contarlo dos veces; los
proyectos genéricos de Civislend se copian al detalle de inversiones privadas con una nota
visible cuando faltan fecha real, vencimiento o rentabilidad prevista.

El comparador de cambios resta por defecto 1 € por vender y otro por comprar, incorpora
el spread, el cambio de divisa y el posible impuesto sobre la plusvalía configurados, muestra
el capital neto reinvertible y cuántas unidades de la alternativa podrían adquirirse.
Además calcula la mejora técnica y el umbral mínimo que debe recuperar la alternativa antes
de que el cambio empiece a compensar sus costes.
Sólo recomienda estudiar un cambio si la posición actual está deteriorada, la alternativa
tiene una entrada atractiva, mejora al menos 10 puntos técnicos y 12 puntos de oportunidad.
Para monedas distintas convierte el importe con el último tipo de referencia del BCE.

Desde Inicio, **Revisar toda mi cartera y buscar oportunidades** ejecuta un recorrido
único: actualiza posiciones y favoritas, abre el Radar y después consulta un universo
externo máximo de 12 small caps líquidas de Nasdaq/NYSE. El screener sólo descubre
nombres; una small cap se descarta si es OTC, cotiza por debajo de 1 USD, negocia menos
de 2 millones USD diarios, tiene cobertura insuficiente, beneficio/riesgo inferior a
2:1, una subida diaria superior al 15% o no supera todos los filtros de Entradas. La
tabla conserva los descartes y sus motivos para evitar seleccionar únicamente ganadoras.

## Alertas por correo

Cada usuario puede guardar su propio correo, activar o desactivar avisos de entrada,
reducción y posible salida, elegir una nota mínima de entrada e incluir el seguimiento
del grupo. El proceso genera dos formatos complementarios. El **resumen diario** reúne
un extracto de hasta 15 empresas, priorizando riesgo de posiciones, cambios y nuevas
candidatas. Los recuentos describen todo el universo solicitado; las omisiones del
extracto se indican y enlazan con la aplicación. Las fuentes ausentes aparecen como
N/D: un fallo de descarga no se presenta como revisión completa.
La **alerta relevante** distingue candidatura condicionada, revisión de exposición y
revisión de tesis. No convierte una mala entrada actual en una venta. Por defecto no
repite el mismo estado; las posiciones que siguen requiriendo revisión permanecen
visibles en el resumen diario.

La revisión automática incluye favoritas y posiciones abiertas. Las entradas se
evalúan únicamente para empresas que no están ya en cartera y sólo generan correo
cuando crecimiento y momentum cumple sus propias reglas y el umbral de la preferencia.
Calidad, valoración, riesgo y el filtro auxiliar se muestran por separado, conservando
sus advertencias y discrepancias; no se exige una nota de consenso. Datos primarios
incompletos no generan una nueva candidata. Las revisiones de exposición o tesis se
limitan a posiciones registradas y respetan su plan explícito o la tesis previa.
El proceso usa configuración predeterminada documentada y precios diarios:
no vigila el mercado en tiempo real ni garantiza que el precio continúe disponible.
La interfaz conserva la fecha del último correo aceptado por SMTP aunque las revisiones
posteriores no tengan novedades, permitiendo distinguir revisión de entrega. También
conserva las notas de crecimiento, fundamentales y oportunidad calculadas en la misma
ejecución del correo.

## Dos enfoques sin nuevas suscripciones

### Revisión manual y envío individual

En **Más → Alertas por correo**, «Revisar mis favoritas y enviarme el resumen»
utiliza exclusivamente las preferencias guardadas del usuario autenticado. No
enumera destinatarios ajenos ni cambia direcciones o posiciones. Requiere canal
SMTP configurado, avisos activados y al menos un análisis fresco utilizable.
El resumen distingue aceptación SMTP de llegada a la bandeja de entrada. La
sesión bloquea dobles clics, reenvíos tras aceptación y reintentos con entrega
incierta; no es una garantía global de entrega exactamente una vez.

Publicar una revisión de interfaz no envía correos ni activa solicitudes de prueba.
El envío individual requiere pulsar el control del usuario autenticado. El workflow
periódico, sus destinatarios y las preferencias existentes se conservan.

La navegación propone **Analizar → Estrategias → Crecimiento y momentum** como lectura
principal y conserva calidad fundamental, ventaja relativa, especulativas, radar y
validación. El contraste de calidad/valoración/tesis/riesgo permanece independiente.
El contexto cíclico y estacional no genera entradas por calendario: sin evidencia se
declara no validado. No se promete rentabilidad ni probabilidad de éxito desde un score.

La vista principal tiene una sola tabla con cuatro filtros, en este orden: **Mi
cartera**, **Entradas fuertes**, **Candidatas** y **Esperar**. Abre primero las posiciones
existentes cuando las hay; las nuevas entradas fuertes y candidatas se mantienen
separadas. «Entrada fuerte» es el nivel más exigente de crecimiento/momentum, no
una afirmación de precio barato ni una orden de compra. Muestra empresa, peso
guardado, señal de entrada, qué revisar, referencia de
cierre/stop teórico, motivo y fecha. Los pesos indican la fecha de la fotografía y
no simulan una sincronización con el bróker. Una señal de entrada insuficiente no
ordena vender lo que ya se tiene; una cartera sin verificar tampoco se interpreta
como vacía. Los valores sin datos permanecen visibles como pendientes. Las notas,
otros enfoques y el dimensionamiento se abren mediante el detalle, mientras las
listas y la configuración de capital/riesgo quedan plegadas. El detalle no habilita
un plan de compra para datos obsoletos ni para una tesis invalidada.

Cada posición puede documentar estrategia, horizonte y tesis mediante un formulario
manual. No se asignan planes ni se borran invalidaciones de tesis al abrir la página.
Los planes se guardan en comentarios ya existentes; las fotografías de análisis
conservan metodología, configuración y un identificador verificable en sus notas.
Ambos backends conservan estos bloques completos, sin una migración de esquema.
El historial reconstruye cada lectura desde su metadata y distingue las revisiones
anteriores que no disponen de ella; no presenta momentum como calidad fundamental.

La versión reutiliza Python, pandas, NumPy, Plotly, los datos gratuitos y la conversión
de referencia del BCE existentes. No añade suscripciones, endpoints de pago ni envío
de órdenes. SEC y EIA son opciones públicas para ampliar evidencia, pero añadir una
fuente no supone que todas las empresas o patrones estén cubiertos. La licencia del
software y los derechos de redistribución de los datos son distintos: yfinance declara
uso personal; debe revisarse antes de ampliar el uso compartido. El análisis diario
no sustituye una orden stop registrada en el bróker ni vigila el mercado en tiempo real.

El laboratorio conserva su comparación de la temporada existente frente a mantener
y al benchmark. La nueva presentación de dos enfoques no convierte retrospectivamente
esa temporada en dos bots validados: cualquier comparación adicional necesitará reglas
registradas antes de observar resultados e historial suficiente. No se selecciona un
ganador retrospectivamente ni se rellenan rentabilidades inexistentes.

El workflow `.github/workflows/daily-alerts.yml` se programa a las 23:00 de Madrid
de lunes a viernes, con recuperación a las 01:17 de martes a sábado. Tolera retrasos
hasta las 05:59 conservando la fecha del cierre anterior y sin duplicar el correo
de una misma sesión. También puede iniciarse manualmente. Véanse los límites de
entrega en `docs/daily_score_digest.md`. Necesita cuatro secretos de GitHub:
`SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `EMAIL_SMTP_USERNAME` y
`EMAIL_SMTP_PASSWORD` (los dos últimos corresponden al Gmail del proyecto; el host,
el puerto y el remitente ya están resueltos en el workflow).

Para probar el envío desde la interfaz, el mismo Gmail se configura en el bloque
`[email]` de los secretos de Streamlit. `EMAIL_SMTP_PASSWORD` y `app_password`
deben ser una contraseña de aplicación de Google, nunca la contraseña normal.
La dirección de cada destinatario se guarda en Supabase y sólo es accesible con la
clave secreta del backend.

## Fuentes de datos

- **Yahoo mediante yfinance:** precios diarios, moneda de cotización, sector y
  múltiplos de valoración. También permite buscar empresas por nombre. Es la fuente
  práctica principal del prototipo.
- **SEC EDGAR:** para tickers estadounidenses sin sufijo, la aplicación intenta
  calcular rentabilidad, márgenes, crecimiento, deuda, liquidez y caja desde
  estados financieros oficiales. Si SEC no está disponible, conserva Yahoo como respaldo.
- **Banco Central Europeo:** tipos de referencia diarios para comparar cambios
  de cartera entre EUR, USD, GBP y otras monedas soportadas.
- **Alpha Vantage:** comprobación opcional del último cierre. La clave se introduce
  en la interfaz y no es necesaria para utilizar el resto de la aplicación.
- **MSN Dinero:** contraste externo por empresa para revisar cotización, resultados,
  previsiones de analistas, inversores y noticias. MSN indica que sus datos financieros
  proceden de LSEG. No se extraen automáticamente ni se incorporan al score porque
  Microsoft no ofrece una API pública estable para reutilizarlos en esta aplicación.

La pantalla muestra la procedencia, la fecha del último periodo oficial y cualquier
discrepancia superior al 1% entre el cierre principal y el alternativo.

## Guías probabilísticas de compra y venta

La aplicación compara el estado actual con eventos históricos de score y tendencia similares,
separados para evitar observaciones solapadas. Con al menos ocho casos muestra retorno mediano,
porcentaje de resultados positivos y rango entre cuartiles para el horizonte seleccionado.
Estas cifras son retrospectivas y no predicciones.

La guía de compra limita cualquier entrada al tamaño máximo calculado por riesgo y propone
esperar, vigilar, una entrada exploratoria, parcial o escalonada. Para posiciones abiertas,
el plan de beneficios divide referencias 1R/2R/3R en tramos del 25%, descuenta la comisión
de cada venta y conserva una parte con protección dinámica. Los niveles no garantizan el
precio real de ejecución y no incluyen impuestos.

La posición se dimensiona así:

```text
fracción invertida = min(100%, riesgo máximo por operación / distancia del stop loss)
```

Por ejemplo, riesgo del 1% y stop del 8% implica una asignación inicial del 12,5% del
capital. Esta simplificación no tiene en cuenta correlaciones entre posiciones.

El capital líquido inicial de referencia es 1.000 €. En cada empresa, el plan de
orden permite introducir un precio límite y calcular la compra por importe —incluidas
fracciones— o por número de acciones. Muestra stop inicial, pérdida monetaria con
comisiones, referencias 1R/2R/3R y la escalera del trailing. Trade Republic restringe
las órdenes stop y límite sobre fracciones; Revolut puede permitir compras por importe,
pero la disponibilidad de órdenes condicionadas sobre fracciones debe comprobarse en
la cuenta concreta. Si el bróker no admite la orden, la aplicación sólo genera una
alerta y la salida sigue siendo manual.

## Hipótesis del backtest

- La señal se calcula al cierre y se ejecuta en la apertura siguiente.
- El modelo es long-only y admite unidades fraccionarias.
- Comisión y deslizamiento se aplican a entradas y salidas.
- Un gap que cruza el stop se ejecuta en la apertura, no al nivel ideal del stop.
- El trailing stop se actualiza con información de sesiones terminadas; los datos diarios
  no permiten conocer con certeza el orden intradía entre máximo y mínimo.
- Buy & hold se normaliza desde el primer cierre del intervalo.

## Riesgos metodológicos

- **Sobreoptimización:** elegir parámetros tras muchas pruebas en la misma muestra puede
  capturar ruido. Deben reservarse datos fuera de muestra y aplicar validación walk-forward.
- **Sesgo de supervivencia:** una lista actual de tickers excluye empresas deslistadas.
  Los backtests de universos históricos requieren constituyentes fechados y esos activos.
- **Look-ahead bias:** cualquier uso prematuro de cierres, máximos o revisiones futuras
  infla los resultados. Esta versión desplaza las órdenes una sesión, pero no reemplaza una
  auditoría con datos intradía para estrategias sensibles a stops.
- **Datos:** ninguna fuente gratuita ofrece garantías profesionales completas. Pueden
  existir huecos, revisiones y diferencias de ajuste. SEC aporta cuentas publicadas,
  pero no estimaciones del futuro; Alpha Vantage es sólo un contraste opcional y
  MSN se consulta de forma manual sin alterar las puntuaciones.
- **Comparabilidad:** los múltiplos tienen significados diferentes por sector y los
  tipos del BCE no incluyen el coste real de conversión del broker.

Ejecuta las pruebas con `pytest -q`.

## Distribución para Windows

Los archivos de construcción están en `packaging/windows`. El instalador crea una
aplicación de escritorio que abre Streamlit en el navegador y guarda la cartera de
cada usuario en `%LOCALAPPDATA%\StockSignalLab`. Puede compilarse desde PowerShell
en Windows o mediante el workflow manual de GitHub Actions
`Construir instalador Windows`.

## Publicación web gratuita

La aplicación está preparada para Streamlit Community Cloud con acceso privado por
contraseña. Consulta [DEPLOYMENT.md](DEPLOYMENT.md). En alojamiento gratuito el
diario SQLite se desactiva deliberadamente porque el disco puede reiniciarse; los
análisis, gráficos y backtests continúan disponibles.

Community Cloud hiberna las aplicaciones gratuitas después de 12 horas sin tráfico.
La primera visita posterior debe despertarla y puede tardar algo más; no significa que
se haya borrado. Las cachés de precios, fundamentales y búsquedas tienen un tamaño máximo
para reducir el riesgo de superar la memoria disponible del alojamiento gratuito.
