# Estabilización de Stock Signal Lab · 3 de octubre de 2026

Cambios implementados en este checkout. No se ha publicado esta versión, ejecutado
migraciones sobre producción, enviado correos reales ni cambiado operaciones del usuario.

**Actualización del 5 de octubre:** la validación posterior alcanza 468 pruebas en
Python 3.12 y 3.13 y 20 comprobaciones SQL ejecutadas en PostgreSQL WASM. Se añadió
una guardia de sesión cerrada al laboratorio. El estado de publicación y sus límites
se detallan en `VALIDACION_PUBLICACION_2026-10-05.md`; los resultados siguientes
corresponden al pase inicial del 3 de octubre.

## Qué queda más claro

Inicio separa **Resumen**, **Decisiones**, **Laboratorio** y **Accesos**. Sólo se
ejecuta la vista seleccionada: abrir el resumen no calcula las rotaciones ni registra
sesiones del laboratorio. Los activos manuales permanecen en el patrimonio, agrupados
en un desplegable, sin mezclarse con las alternativas cotizadas.

Inicio prioriza actualizar los precios de posiciones reales y virtuales. Las favoritas
tienen revisión completa explícita y el correo conserva su proceso programado separado.
La pantalla distingue precio reciente, valor manual y dato pendiente.

## Correcciones que afectan a dinero y decisiones

- Fotografías reconciliadas por cuenta y ticker: vender en un bróker no elimina una
  posición del mismo valor en otro. Las fechas se conservan por cuenta y las ventas
  parciales reducen proporcionalmente el valor declarado cuando falta cotización.
- Costes por lotes FIFO y liquidación real compartidos entre diario, resumen e historial.
  Las comisiones no se descuentan dos veces. Sin compras suficientes, costes o precios,
  el resultado agregado queda pendiente en lugar de presentar una ganancia ficticia.
- Los porcentajes aproximados explican su denominador. No se presentan como TWR,
  XIRR ni rentabilidad anual completa; el historial separa el resultado incremental
  del año del acumulado.
- Correo y aplicación utilizan el mismo criterio de decisión de cartera. Un momento
  técnico bajo, por sí solo, pide confirmación y no una venta. Los correos muestran
  fecha, cobertura y motivo; los datos antiguos no generan nuevas alertas de salida.
- Supabase pagina los historiales y sustituye el reemplazo destructivo de fotografías
  por una sola transacción. Si falta la migración RPC, conserva los datos y explica
  la actualización necesaria.
- El laboratorio mide cobertura de estrategia, cartera original e índice. No anuncia
  mejora relativa cuando alguna referencia está incompleta. Tampoco usa divisas
  actuales para simular ejecuciones antiguas ni una venta posterior para financiar
  una compra anterior en otro mercado. Las operaciones no verificables quedan pendientes.
- El comparador bloquea rentabilidades relativas en monedas diferentes o sin verificar.
  La calibración histórica cuenta bloques temporales, no múltiples tickers de la misma
  fecha como observaciones independientes. Sus frecuencias son descriptivas.

## Verificación

- **448 pruebas pasan** en un entorno temporal limpio, con las dependencias del proyecto.
- Las pruebas de interfaz ejercitan las cuatro vistas de Inicio y comprueban que las
  otras vistas no se ejecutan ocultas.
- El esquema y las dos migraciones nuevas pasan el análisis sintáctico SQL. No se
  han ejecutado sobre un servidor PostgreSQL en este turno.
- La revisión visual mediante capturas de navegador sigue pendiente: Chrome no pudo
  arrancar en el entorno disponible. No se da por verificado el aspecto real en móvil.

## Antes de publicar

1. Probar con respaldo las dos migraciones nuevas indicadas en `DEPLOYMENT.md`:
   `migration_atomic_portfolio_snapshot.sql` y `migration_paper_comparator_coverage.sql`.
2. Comprobar en una base de ensayo el reemplazo de una fotografía, los historiales
   paginados y la lectura/escritura de una sesión del laboratorio.
3. Revisar visualmente ordenador y móvil, y después desplegar y verificar la aplicación
   autenticada. La versión actual en producción no contiene estos cambios todavía.

## Límites que no se esconden

La actualización de portada ocurre al cargar la aplicación; el correo tiene su tarea
diaria y el laboratorio registra al abrir su vista. Esto no crea un servicio nuevo que
guarde automáticamente todos los saldos y simulaciones cada noche.

No se ha incorporado un proveedor de divisas históricas ni un calendario completo de
festivos y aperturas. Los casos que necesiten esos datos quedan pendientes. Las parejas
internacionales no verificadas no se ejecutan y no hay ejecución escalonada entre días.
La frescura de precios tolera hasta dos días laborables, sin atribuir precisión intradía.

Los resultados siguen dependiendo de las operaciones registradas y de los datos del
proveedor. Ninguna señal ni simulación garantiza beneficios o sustituye una decisión
revisada por el usuario. No se envían órdenes a los brókeres.
