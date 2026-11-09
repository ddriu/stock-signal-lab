# Actualización reanudable del Radar

La acción **Actualizar ahora todas mis favoritas** toma una lista fija de
símbolos (favoritas y posiciones de la vista), fecha, hora y ajustes.
No hace solicitudes al proveedor por el mero hecho de abrir la vista.

- Procesa seis símbolos por bloque y confirma cada bloque en el diario
  antes de continuar. La pestaña sólo pide el siguiente bloque; no es la
  fuente del avance. El Radar no se queda ejecutando en segundo plano al
  cerrar la página.
- **Reanudar pendientes** continúa una revisión del mismo día y ajustes.
  **Reintentar solo las incidencias** vuelve a consultar los símbolos con
  cobertura incompleta, sin precios válidos o sin guardado. No repite los
  completos ni reintenta automáticamente en bucle.
- Se distinguen cobertura completa, parcial, cierre anterior y problema de
  guardado. Un dato guardado previamente no prueba que la consulta actual
  haya tenido éxito. La última lectura válida se conserva como referencia.
- La fecha del cierre no es la hora de revisión. Se usan cierres ya
  publicados según el calendario del símbolo; los comparadores no miran
  más allá de la fecha del cierre analizado. No son precios intradía.
- Dos pestañas no pueden reservar el mismo bloque simultáneamente. Si se
  interrumpe el proceso dentro de un bloque, su reserva caduca en cinco
  minutos. Los bloques confirmados quedan disponibles para ese usuario.

No cambia las reglas de inversión, las listas, la cartera, los destinatarios
ni los informes diarios archivados. El estado de revisión no autoriza compras
ni envía correos.

## Publicación

Primero se debe verificar y aplicar en el proyecto correcto la migración
`supabase/migrations/20261109110626_radar_review_checkpoints.sql`. Añade una
tabla privada y una operación de escritura atómica con permisos de servidor;
no modifica las observaciones, cartera ni preferencias existentes. Después,
desplegar la aplicación y comprobar el arranque y una revisión de prueba.
Si la migración no está disponible, el Radar no inicia una revisión que no
pueda guardar. No es necesario recrear ni reiniciar la base de datos.

Pruebas locales: `tests/test_radar_review.py`, `tests/test_radar_review_storage.py`,
`tests/test_radar_market_loading.py`, `tests/test_opportunity_catalog.py` y
`node scripts/validate_radar_review_migration.mjs .`. Los proveedores están
simulados en estas pruebas; no garantizan que una fuente tenga datos de todos
los mercados.
