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
│   ├── growth_momentum.py    # Estrategia mensual dinámica y perfiles sectoriales
│   ├── staircase_projection.py # Aportaciones, escalera y simulación a diez años
│   ├── sector_comparison.py  # Comparación normalizada y liderazgo entre pares
│   ├── backtesting.py        # Simulador long-only sin anticipación
│   ├── visualization.py      # Gráficos Plotly
│   ├── ui.py                 # Diseño responsive y perfiles de configuración
│   ├── risk.py               # Tamaño orientativo de posición y riesgo monetario
│   ├── portfolio.py          # Valoración y comparación de cambios
│   ├── portfolio_history.py  # Evolución diaria y resumen anual de cartera
│   ├── portfolio_export.py   # Libro Excel con tablas y gráficos editables
│   ├── portfolio_snapshot_import.py # Fotografías XLSX sin inventar operaciones
│   ├── portfolio_snapshot.py # KPIs de la última fotografía sin mezclar fechas
│   ├── navigation.py         # Estado seguro entre favoritos y análisis directo
│   ├── segofactoring_import.py # Importación idempotente del resumen XLSX
│   ├── recommendations.py    # Entradas, retornos históricos y ventas parciales
│   ├── return_calibration.py # Probabilidad histórica de superar objetivos a 30+ días
│   └── journal.py            # Diario SQLite y exportación mediante la UI
└── tests/                    # Pruebas unitarias sin red
```

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

La portada reúne primero el valor de la cartera, el resultado latente, las posiciones,
el seguimiento y las alertas del día. Cuando existe una fotografía importada, muestra
sus cifras aunque todavía no haya operaciones reconstruidas, junto con gráficos por
plataforma y por activos. La navegación separa inicio, análisis,
favoritos, carteras y guía para cargar únicamente la sección que se está consultando.
Las oportunidades se presentan en tarjetas fáciles de leer y conservan el ranking
completo y los gráficos técnicos como detalle desplegable.
Dentro de **Analizar** hay además un desplegable de favoritas y un buscador con
autocompletado sobre favoritas, análisis guardados y empresas abiertas recientemente.
En **Favoritos**, cada fila concentra sus acciones de análisis, etiquetas y borrado;
ya no existe un segundo selector separado al final de la lista. La consulta diaria y
el formulario para añadir empresas aparecen como dos vistas separadas para reducir ruido.

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
La pestaña independiente **Proyección de capital** sí los muestra como bolsas separadas
para explicar el plan mensual sin mezclarlo con la selección de empresas:
capital actual, 250 euros mensuales de Civislend, 250 de facturas y el resto entre
acciones tradicionales y escalera. La escalera sólo aumenta su porcentaje después de
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
el coste medio incluyendo las comisiones, admite ventas parciales y calcula beneficio realizado
y beneficio neto si se vendiera al último cierre. Las posiciones guardadas se añaden
automáticamente a la próxima descarga.

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
Las posiciones abiertas —también cuando superan 50— se actualizan automáticamente con
aproximadamente 14 meses de precio y tendencia, evitando solicitar cuentas empresariales
innecesarias para toda la cartera. El historial de operaciones no tiene ese límite.

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
marca como suficiente desde 30 casos no solapados e incluye un intervalo de incertidumbre
del 95%. La calibración utiliza el score técnico histórico; no aplica fundamentales
actuales a fechas pasadas porque eso introduciría *look-ahead bias*.

Desde la ficha de una empresa se puede guardar una fotografía privada del análisis:
precio, fecha, seis notas, lecturas, expectativa histórica y una nota personal. El
historial permite ver la evolución, abrir de nuevo la empresa y borrar revisiones.
No duplica las series de precios ni las gráficas en la base de datos.

Cada cuenta mantiene una **cartera privada** separada. Además existe una **cartera del
grupo**, visible únicamente después de iniciar sesión, en la que los cuatro miembros
pueden registrar las decisiones comunes. Cada movimiento compartido identifica quién
lo añadió; un usuario puede eliminar los suyos y el administrador puede corregir cualquiera.
Ambas carteras muestran capital pendiente, valor neto, beneficio latente y realizado,
rentabilidad, comisiones y cobertura de precios. El rol administrador también ve un
resumen de las cuatro carteras privadas y puede registrar operaciones para un usuario.

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

El comparador de cambios resta por defecto 1 € por vender y otro por comprar,
muestra cuántas unidades de la alternativa podrían adquirirse y calcula la mejora técnica.
Sólo recomienda estudiar un cambio si la posición actual está deteriorada, la alternativa
tiene una entrada atractiva, mejora al menos 10 puntos técnicos y 5 puntos de oportunidad.
Para monedas distintas convierte el importe con el último tipo de referencia del BCE.
No incluye fiscalidad, spread ni el margen de cambio aplicado por el broker.

## Alertas por correo

Cada usuario puede guardar su propio correo, activar o desactivar avisos de entrada,
reducción y posible salida, elegir una nota mínima de entrada e incluir el seguimiento
del grupo. El correo contiene un solo resumen y, por defecto, sólo se envía cuando la
categoría cambia para evitar mensajes repetidos.

La revisión automática incluye favoritas y posiciones abiertas. Las entradas se
evalúan únicamente para empresas que no están ya en cartera; las alertas de reducir
o vender se limitan a posiciones registradas y utilizan su coste medio para comprobar
el stop loss. El proceso usa el perfil equilibrado predeterminado y precios diarios:
no vigila el mercado en tiempo real ni garantiza que el precio continúe disponible.

El workflow `.github/workflows/daily-alerts.yml` se ejecuta por la mañana de lunes
a viernes y también puede iniciarse manualmente. Necesita cuatro secretos de GitHub:
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
