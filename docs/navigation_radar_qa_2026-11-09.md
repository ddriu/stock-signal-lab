# Stock Signal Lab: navegación y recuperación del Radar

- Base de interfaz y comparación visual: `6c8b5ef`.
- Base antigua que se conserva en memoria al reproducir la incompatibilidad: `2377afc`.
- Implementación probada en navegador: `2c36884`, rama local
  `codex/navigation-radar-qa`. El cambio en el procesamiento de Radar es
  `40dbbc5`; después se ajustaron el ancho de Cuenta en móvil y el guion de QA.
- Entorno de navegador: Chrome real, Streamlit 1.66.1, servidor en loopback,
  dos cuentas sintéticas, SQLite temporal y proveedores simulados. No usa
  secretos ni conexiones del despliegue.

## Mapa

| Pestaña fija | Vista al entrar | Accesos relacionados |
|---|---|---|
| Hoy | Resumen de cartera y posibles entradas, con fecha de revisión | Por horizonte y cinco sesiones |
| Cartera | Mi cartera privada | Plan por posición, cartera del grupo y plan de capital |
| Radar | Favoritas, lectura guardada y avance de actualización | Entradas hoy, Empresa y Comparar |
| Favoritas | Mis listas | Añadir empresa, con destino privado o grupo |
| Más | Índice de tareas avanzadas | Resultados, laboratorio, alertas, métodos, validación y administración según el rol |

`Cuenta` vive en la cabecera y contiene la única acción de cierre de sesión.
Los ajustes de análisis son un control local plegado; no hay controles de
trabajo en la barra lateral. Los destinos principales no cambian al navegar.

El navegador conserva las rutas al volver y al recargar. Se puede abrir una
empresa directamente con `/empresa?ticker=NVDA`; entrar por ese enlace no
descarga cotizaciones. Al abrir una pestaña principal se muestra su vista de
entrada, no la última subpantalla visitada. Los ajustes y filtros de lectura
se pueden conservar durante la sesión; cerrar sesión elimina el contexto
privado. Si la sesión caduca, el acceso vuelve a pedir credenciales y conserva
la ruta directa. `?digest=<id>` busca solo ese informe en la cuenta autorizada;
«Volver a Hoy» sale del archivo sin eliminar el parámetro de versión.

## Corrección de la incompatibilidad

La prueba conserva módulos y adaptadores del diario cargados desde el código
real de `2377afc` y ejecuta las llamadas nuevas. Sin reparación se reproducen
errores de argumento inesperado, incluido `verified_review=`. La versión nueva
comprueba los contratos de los módulos que consume Radar y actualiza los que
son incompatibles; reemplaza las instancias antiguas del diario sin borrar
los datos del usuario ni vaciar todas las cachés. No se inspeccionaron los
registros de Streamlit Cloud, así que esta reproducción confirma el mecanismo
del fallo, no el estado interno concreto del proceso de producción.

Mi Radar abre el catálogo guardado y no carga series históricas ni recalcula
sus notas por navegar. La acción explícita guarda cada bloque; si Streamlit
interrumpe la ejecución al cambiar de página, se libera la reserva del bloque
no confirmado. Al reanudar, se reutilizan los resultados confirmados. En la
ficha de empresa se distinguen nueva entrada, posición existente, fecha del
dato y cobertura; los gráficos se muestran a petición.

Una primera pasada detectó que la recarga automática entre bloques podía
competir con el clic de navegación. La revisión publica el avance sin
reiniciar toda la página; los cambios de vista interrumpen el trabajo
pendiente sin deshacer lo que ya se ha confirmado.

## Evidencia de aceptación

Las capturas y los registros `before-chrome.json`, `after-chrome.json` y
`warm-chrome.json` se entregan aparte. Solo contienen datos sintéticos.
La consulta externa de prueba es independiente de estos recorridos.

| Comprobación | Versión y entorno | Pasos, resultado y evidencia |
|---|---|---|
| Incompatibilidad | `2377afc` → `2c36884` · pytest y Streamlit AppTest | Con módulos y diario antiguos todavía cargados se reproduce el `TypeError`; el nuevo Radar conserva la verificación y abre con una sesión de prueba. `tests/test_radar_runtime_compat.py`. |
| Suite automática | `40dbbc5` completa, `2c36884` dirigida · pytest | 2.119 pruebas pasaron tras corregir la carrera de bloques; 70 de arranque, navegación, Radar y compatibilidad pasaron también tras el ajuste final de interfaz. |
| Comparación visual | `6c8b5ef` · Chrome local autenticado | Capturas `before-desktop-radar.png` y `before-mobile-radar.png`. El recorrido antiguo usa el desplegable y tres selecciones; la nueva barra permite entrar con un clic. |
| Actualización en caliente | `2377afc` → `2c36884` · Chrome local autenticado, mismo proceso | Con la versión antigua cargada se abre su Radar mediante un control exclusivo de QA. Se actualiza el código sin reiniciar Python ni cerrar sesión, se entra en Radar por la barra nueva y se guardan dos bloques (12 resultados). Se registra la incidencia del proveedor, se sale a Favoritas y se vuelve a Radar; muestra la opción de reanudar. `warm-chrome.json`, `warm-radar-before-switch.png`, `warm-radar-after-switch.png`. |
| Rutas y accesibilidad | `2c36884` · Chrome local autenticado, escritorio y móvil emulado de 390 px | Hoy, Radar, Empresa, Favoritas, Cartera, Más y Cuenta; enlace directo a la ficha, Atrás/Adelante y Tab/Enter. En móvil, controles de navegación de 44 px y sin desbordamiento horizontal de página. Sin barra lateral operativa; un único cierre de sesión en Cuenta. `after-chrome.json`. |
| Reanudación, datos parciales y actualización individual | `2c36884` · Chrome local autenticado + inspección SQLite de solo lectura | Primer bloque de 6 confirmado; salida del Radar y reconexión; reanudadas solo las pendientes. Resultado: 140 completas, una sin datos y una con cierre antiguo. Ninguna de las seis se solicitó de nuevo. La actualización de empresa pidió un solo valor más sus referencias de contexto. `after-chrome.json`, `after-mobile-radar-paused.png`, `after-mobile-radar-finished.png`. |
| Separación de cuentas | `2c36884` · Chrome local, dos sesiones de prueba | La segunda cuenta solo vio sus dos valores y el grupo vacío; el enlace a un informe archivado de la primera no reveló sus valores. `after-chrome.json`. |
| Fluidez | `6c8b5ef` y `2c36884` · Chrome local | Hoy → Radar, antes: 3,23 s escritorio y 2,96 s móvil (recorrido de varios pasos). Después: 0,57 s escritorio y 0,23 s móvil; los demás cambios de vista medidos en escritorio tardaron 0,17–0,53 s. Cero peticiones a proveedores al navegar. Actualización explícita aparte: primer bloque 1,30 s y reanudación 22,05 s con proveedor simulado. Son observaciones locales, no tiempos garantizados en la nube. `before-chrome.json`, `after-chrome.json`. |
| Consulta limitada al proveedor | Yahoo Finance mediante `download_prices` · sin almacenamiento | NVDA, 01/09–06/11/2026: 48 sesiones, fechas únicas y cierres positivos. No es una prueba de cobertura de todas las favoritas ni se utilizó para la prueba de reanudación. |
| Producción | Streamlit Cloud · sin acceso a sesión de prueba separada en ese despliegue | No desplegado ni verificado. No se usa el acceso a un formulario de login como evidencia de aceptación. |

La prueba de navegador se puede reproducir en macOS con Chrome instalado y
Node 26, sin paquetes npm: arrancar `scripts/qa/navigation_fixture.py` con
Streamlit en `127.0.0.1`, apuntar `SSL_QA_SOURCE` al checkout y
`SSL_QA_DATA_DIR` a un directorio nuevo `/private/tmp/ssl-nav-*`. Ejecutar
`node scripts/qa/navigation_chrome.mjs <url-loopback> <datos> <evidencia> after`.
Para la prueba en caliente, proporcionar también `SSL_QA_PREVIOUS_SOURCE`
con un archivo extraído de `2377afc` y ejecutar el modo `warm`.

La rama no se ha subido ni publicado. No se han hecho pruebas con usuarios
reales en Streamlit Cloud. Tras aprobación, verificar la revisión exacta
desplegada y una ruta autenticada de Radar en un entorno autorizado antes
de declarar verificada la producción.
