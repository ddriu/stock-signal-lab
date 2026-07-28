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
│   ├── data_loader.py        # yfinance, validación y normalización OHLCV
│   ├── data_sources.py       # SEC EDGAR, BCE, Alpha Vantage y trazabilidad
│   ├── auth.py               # Cuentas, roles y contraseñas con hash PBKDF2
│   ├── dashboard.py          # KPIs y valoración común de carteras
│   ├── indicators.py         # SMA, RSI, MACD, volumen y ATR
│   ├── signal_engine.py      # Momento de entrada y gestión de posición
│   ├── fundamentals.py       # Calidad empresarial con cobertura de datos
│   ├── opportunity.py        # Valoración, fortaleza relativa, riesgo y score conjunto
│   ├── backtesting.py        # Simulador long-only sin anticipación
│   ├── visualization.py      # Gráficos Plotly
│   ├── risk.py               # Tamaño orientativo de posición y riesgo monetario
│   ├── portfolio.py          # Valoración y comparación de cambios
│   ├── recommendations.py    # Entradas, retornos históricos y ventas parciales
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

## Cartera y cambios

El diario guarda compras y ventas con fecha, cantidad, precio, moneda y comisión. Reconstruye
el coste medio incluyendo las comisiones, admite ventas parciales y calcula beneficio realizado
y beneficio neto si se vendiera al último cierre. Las posiciones guardadas se añaden
automáticamente a la próxima descarga.

Cada cuenta mantiene una **cartera privada** separada. Además existe una **cartera del
grupo**, visible únicamente después de iniciar sesión, en la que los cuatro miembros
pueden registrar las decisiones comunes. Cada movimiento compartido identifica quién
lo añadió; un usuario puede eliminar los suyos y el administrador puede corregir cualquiera.
Ambas carteras muestran capital pendiente, valor neto, beneficio latente y realizado,
rentabilidad, comisiones y cobertura de precios. El rol administrador también ve un
resumen de las cuatro carteras privadas y puede registrar operaciones para un usuario.

El comparador de cambios resta por defecto 1 € por vender y otro por comprar,
muestra cuántas unidades de la alternativa podrían adquirirse y calcula la mejora técnica.
Sólo recomienda estudiar un cambio si la posición actual está deteriorada, la alternativa
tiene una entrada atractiva, mejora al menos 10 puntos técnicos y 5 puntos de oportunidad.
Para monedas distintas convierte el importe con el último tipo de referencia del BCE.
No incluye fiscalidad, spread ni el margen de cambio aplicado por el broker.

## Fuentes de datos

- **Yahoo mediante yfinance:** precios diarios, moneda de cotización, sector y
  múltiplos de valoración. Es la fuente práctica principal del prototipo.
- **SEC EDGAR:** para tickers estadounidenses sin sufijo, la aplicación intenta
  calcular rentabilidad, márgenes, crecimiento, deuda, liquidez y caja desde
  estados financieros oficiales. Si SEC no está disponible, conserva Yahoo como respaldo.
- **Banco Central Europeo:** tipos de referencia diarios para comparar cambios
  de cartera entre EUR, USD, GBP y otras monedas soportadas.
- **Alpha Vantage:** comprobación opcional del último cierre. La clave se introduce
  en la interfaz y no es necesaria para utilizar el resto de la aplicación.

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
  pero no estimaciones del futuro; Alpha Vantage es sólo un contraste opcional.
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
