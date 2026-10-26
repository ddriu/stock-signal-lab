# Puntuaciones diarias: una vista y un solo correo

La estrategia original y su configuración no cambian. El informe muestra la
salida observada de crecimiento + momentum y conserva los demás enfoques por
separado. Una nota baja para comprar no ordena vender una posición existente.

## Lectura

- Cartera primero; después favoritas y candidaturas.
- Cinco sesiones reales cerradas del mercado de cada ticker, con fechas.
- Un hueco es «sin dato», nunca una nota reconstruida o arrastrada.
- Los cambios de perfil, versión, fuente o base contable se distinguen de una
  pérdida de fuerza del precio. No se comparan como si fueran iguales.
- La periodicidad y antigüedad de las métricas se muestran sin retocar scores.
- Una nota es una regla del modelo, no una probabilidad de ganar.

## Versionado y privacidad

El informe exacto se guarda antes del correo. Su SHA-256 cubre todo el contenido,
incluidos los avisos. El enlace `?digest=<identificador>` abre esa versión, no
un cálculo posterior. El backend filtra siempre por el usuario autenticado.
Un identificador no concede acceso a los informes de otro propietario.

Las tablas nuevas tienen RLS y permisos exclusivamente del backend
`service_role`. No se publican informes privados en GitHub ni se guardan claves,
destinatarios o mensajes completos de error SMTP en las entregas.

## Horario y entrega

Se reutiliza el workflow existente: 23:00 en Europe/Madrid, lunes a viernes,
adaptado al cambio horario. GitHub Actions puede retrasar la ejecución; no se
promete entrega al minuto. Sin ninguna sesión nueva completada, no se repite
el resumen anterior. Los push/PR no envían correos de prueba.

Antes de SMTP se reserva una clave por usuario, destinatarios, sesión y tipo.
Las reejecuciones y trabajadores concurrentes no reciclan la reserva. Si SMTP
queda incierto, se conserva para evitar duplicados: **como máximo un intento**,
no entrega exactamente una vez. Un fallo incierto requiere revisión manual.
Las preferencias y destinatarios existentes se conservan.

## Validación y publicación

La migración aditiva ya está instalada, sin informes privados ni entregas nuevas
creados por las pruebas. El runner falla de forma cerrada si no puede guardar
el informe o reservar el envío. Publicar el código es una acción separada:
hasta entonces la versión alojada conserva su funcionamiento anterior.

Para revertir, restaurar el código previo y conservar las tablas como evidencia,
sin borrar historiales ni reciclar reservas.
