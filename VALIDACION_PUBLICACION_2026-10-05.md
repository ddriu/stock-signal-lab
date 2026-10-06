# Validación y preparación de publicación · 5 de octubre de 2026

**Estado: validación local completada y migración de Supabase aplicada;
publicación del código pendiente de autorización de GitHub.** No se ha publicado
esta versión, enviado correos reales ni cambiado los datos financieros de la cartera.

## Evidencias

- **468 pruebas pasan en Python 3.12** (24,34 s) y **468 en Python 3.13** (23,03 s),
  usando dos entornos temporales independientes con los requisitos del proyecto.
  Python 3.12 coincide con la versión configurada en GitHub Actions; no se da por
  ejecutado un workflow remoto que todavía no contiene estos cambios.
- La sintaxis de `app.py`, `src/*.py` y `scripts/*.py` es correcta en Python 3.12.
- **20 grupos de comprobaciones SQL pasan** en PostgreSQL 18.3 mediante PGlite 0.5.8:
  migraciones repetibles, datos antiguos preservados, permisos de roles, errores que
  revierten el reemplazo completo, aislamiento por usuario/fecha y bundle transaccional.
- AppTest con SQLite temporal real valida las cuatro vistas de Inicio, horizontes,
  callbacks, temporada con las dos cuentas, reentrada sin duplicación, datos incompletos,
  migración del diario antiguo y reconstrucción de una temporada parcial. No utiliza
  datos del usuario ni llamadas al proveedor.
- El código extraído del paquete ZIP supera de nuevo las **468 pruebas** en Python
  3.12 y las **20 comprobaciones SQL**. Se ha comprobado su integridad y la ausencia
  de rutas de secretos, bases de datos, Git y cachés.

## Última corrección

El laboratorio podía registrar una barra intradiaria como resultado diario y después
no revisarla al cierre. Ahora la guardia del ancla SPY retiene el registro de la sesión
actual hasta las **16:30 de Nueva York**, adapta el cambio de hora y rechaza fechas
futuras o de fin de semana. Antes del umbral conserva el histórico, no inicia/reconstruye
temporadas ni procesa operaciones virtuales. La protección también se aplica al método
de persistencia, no sólo al botón de la interfaz.

Es un margen prudente basado en los [horarios oficiales de NYSE](https://www.nyse.com/trade/hours-calendars),
no un calendario completo ni una garantía de que el proveedor haya finalizado su barra.
Las pruebas incluyen cambios de hora, medianoche UTC y acciones de interfaz antes y
después del umbral.

## Material preparado

- `supabase/release_stabilization_20261005.sql`: instala las dos migraciones juntas,
  comprueba el esquema antes y después, usa una transacción, verifica permisos y
  solicita la recarga de PostgREST. No elimina ni cambia filas existentes. Ejecutarlo
  completo sobre la base correcta de Stock Signal Lab, con un respaldo disponible.
- `scripts/validate_migrations.mjs`: ensayo reproducible sin secretos ni conexión
  externa. El workflow **Pruebas y estabilidad** lo incorpora como un job separado.
- `dist/StockSignalLab-stabilization-20261005.zip`: código, pruebas y documentación
  preparados para la entrega. Excluye Git, entornos, cachés, bases de datos, archivos
  de cartera y la configuración real de secretos. No es un instalador ni un despliegue.

## Supabase: migración aplicada y verificada

La conexión SQL volvió a funcionar después de las comprobaciones de acceso. No se
resetearon contraseñas ni claves, ni se reinició el proyecto ni se concedieron permisos
públicos sobre la cartera. No se atribuye una causa demostrada al fallo previo.

- Proyecto verificado: `ctjhmmzlvlrmhbpgubpc`, Stock Signal Lab, PostgreSQL 17.6.
- Respaldo privado local previo de las dos tablas afectadas y sus metadatos, con
  permisos de archivo restringidos, en `data/private-backups/`. Esta ruta está
  ignorada por Git y excluida del ZIP; no es un respaldo completo del proyecto.
- El bundle completo `release_stabilization_20261005.sql` se aplicó correctamente
  mediante la herramienta de migraciones. Registro:
  `20261005173811` / `stabilization_20261005`.
- Huellas de contenido antes/después idénticas: **175** registros en
  `portfolio_snapshots` y **1** en `paper_daily_runs`. Para comparar el contenido
  previo de esta última se excluyeron únicamente las dos columnas nuevas.
- RPC instalada como `SECURITY INVOKER`, con `search_path=public, pg_temp`.
  `EXECUTE` permitido a `service_role` y propietario, denegado a
  `anon`, `authenticated` y `PUBLIC`.
- Ambas columnas de cobertura son `double precision`, admiten `NULL`, no tienen
  valor por defecto y conservan sus restricciones validadas entre 0 y 100.
  El registro antiguo permanece con cobertura desconocida, sin inventar valores.
- Se conservaron RLS y la denegación de lectura pública. El bundle solicitó la
  recarga de esquema de PostgREST; no se ejecutó la RPC contra datos reales para
  simular una prueba de escritura.
- Advisors de seguridad sin avisos nuevos: permanecen 10 INFO de RLS sin políticas,
  propios del acceso backend, y los dos WARN previos sobre la función de evento
  `rls_auto_enable()` para anon/authenticated. No se alteró esa función del proyecto.

## Acceso necesario para terminar

Las integraciones de **GitHub** y **Supabase** están autenticadas. GitHub permite
leer el repositorio correcto, pero escribir devuelve `403: Resource not accessible
by integration`; no aparece una instalación autorizada sobre el repositorio. El
punto de partida es `a7075df`. La revisión del
paquete ha detectado ejemplos financieros que se han sustituido por datos ficticios
en la interfaz y en las pruebas antes de publicarlos. El Git compartido original y el
archivo local de secretos siguen bloqueados por el sistema; no se elude ese bloqueo.

Tras la confirmación del usuario se comprobó otra vez GitHub: escribir un blob sigue
devolviendo el mismo 403. No se creó rama, commit ni pull request. El acceso de lectura
y los permisos del propietario no demuestran autorización de escritura de la integración.
Falta:

1. Autorizar la integración de GitHub para escribir en este repositorio y publicar
   el código revisado sin sobrescribir cambios ajenos; comprobar
   los workflows y verificar el reinicio de Streamlit.
2. Revisar la aplicación autenticada en ordenador y móvil, incluidos scroll y CSS.

## Qué no demuestra esta validación

AppTest comprueba comportamiento, no una captura visual real. Chrome no pudo arrancar
en el entorno disponible y no se da por revisado el diseño final en móvil. PostgreSQL
WASM monoproceso no demuestra contención entre conexiones simultáneas ni funcionamiento
de escritura de PostgREST. El esquema, permisos y preservación de filas sí se verificaron
en producción; no se han verificado los secretos locales de Streamlit, la escritura
real de la RPC desde la app ni el despliegue del código nuevo.

La aplicación sigue sin enviar órdenes al bróker. Estas pruebas acreditan contratos y
protecciones del software, no rentabilidades futuras ni beneficio garantizado.
