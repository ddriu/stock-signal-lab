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

## Señal y verificación, por separado

La tabla principal muestra la señal original del motor, la suficiencia de sus
datos y el siguiente paso. Una «Entrada fuerte» bloqueada sigue visible como
señal del motor con **datos pendientes / compra no confirmada**. No se convierte
en una candidata verificada, ni su falta de datos significa vender. Las fuertes
con datos suficientes tampoco son órdenes: quedan pendientes de precio, evento,
costes, peso y riesgo. Los contadores distinguen ambas situaciones.

El histórico de cinco sesiones se consulta en el detalle, sin repetir cinco
fechas en cada fila. El correo usa las mismas lecturas del informe, incluye el
número de señales fuertes pendientes y muestra hasta tres, incluso si los avisos
de posiciones ocupan el resumen. Las favoritas restantes están en un desplegable.

La recuperación de fundamentales consulta cuentas fechadas aunque la ficha del
proveedor ya contenga ratios. El valor recalculado y su periodo se sustituyen
juntos; nunca se asigna una fecha general a un ratio distinto. SEC conserva
prioridad, y el respaldo Yahoo queda identificado como **anual**, no trimestral.
No se inventa flujo de caja libre sin inversión de capital, ni se certifican
ratios con unidades o ejercicios incompatibles. Un dato imposible de recuperar
permanece pendiente. La caché incorpora la versión del método, y una revisión
manual vuelve a consultar los fundamentales junto con los precios.

El método de recuperación y los periodos por métrica se conservan en las nuevas
revisiones. Los informes archivados no se reescriben; un cambio de procedencia o
de cifras puede impedir comparar directamente la nota con la sesión anterior.
La vista de crecimiento comparte el control de datos del resumen y no habilita
un plan de entrada mientras falte verificarlo. No cambian los parámetros del
motor, sus etiquetas, umbrales ni reglas de salida.

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
promete entrega al minuto. Los arranques entre medianoche y las 05:59 conservan
la fecha objetivo de la tarde anterior; el viernes puede completarse el sábado.
Existe un intento de recuperación a las 01:17, de martes a sábado, en el mismo
workflow y sujeto a la misma reserva persistente. No es un segundo resumen.
Después de las 06:00, un arranque programado fuera de ventana termina con error
visible en Actions, no con un falso éxito. Sin ninguna sesión nueva completada,
no se repite el resumen anterior. Los push/PR no envían correos de prueba.

La fecha objetivo limita las descargas y la actualidad de las señales. La fecha
de generación y los controles de cierre conservan el reloj real: no se retrodata
el informe ni se mezclan cotizaciones asiáticas de la jornada siguiente.

Antes de SMTP se reserva una clave por usuario, destinatarios, sesión y tipo.
Las reejecuciones y trabajadores concurrentes no reciclan la reserva. Si SMTP
queda incierto, se conserva para evitar duplicados: **como máximo un intento**,
no entrega exactamente una vez. Un fallo incierto requiere revisión manual.
Las preferencias y destinatarios existentes se conservan.

Si el primer intento falla o queda incierto **después de reservar el envío**, la
recuperación no lo reenvía automáticamente. Así se evita duplicar un mensaje que
SMTP pudo aceptar. El intento de recuperación cubre arranques omitidos o fallos
anteriores a la reserva; no garantiza la entrega en la bandeja de entrada.

## Validación y publicación

La migración aditiva ya está instalada, sin informes privados ni entregas nuevas
creados por las pruebas. El runner falla de forma cerrada si no puede guardar
el informe o reservar el envío. Publicar el código es una acción separada:
hasta entonces la versión alojada conserva su funcionamiento anterior.

Para revertir, restaurar el código previo y conservar las tablas como evidencia,
sin borrar historiales ni reciclar reservas.
