# Validación de resúmenes retrasados — 28/10/2026

Estado: código local validado; publicación pendiente de confirmación. Base
publicada: `38c924462b581302f7466bc31882015fa8c2cee9`.

## Incidencia reproducida

El control anterior sólo aceptaba las 23:00–23:59 de Madrid. Los arranques
observados de 27/10 a las 01:05:44 UTC y 28/10 a las 00:58:19 UTC salieron con
código cero antes del análisis y del envío. Permitir esas horas sin conservar
la fecha objetivo habría exigido incorrectamente una cotización del nuevo día.

## Corrección

- Resolver la sesión objetivo entre las 23:00 y las 05:59 de Madrid, incluido
  el cierre del viernes procesado el sábado. Fuera de ventana se muestra un
  error en Actions, antes de acceder a usuarios o SMTP.
- Pasar esa fecha al runner para limitar descarga, actualidad e informe.
  Los controles de cierre y la hora de registro conservan el reloj real.
- Mantener las 23:00 L–V y añadir recuperación a las 01:17 M–S, sin cambiar
  destinatarios ni preferencias. La reserva previa a SMTP sigue siendo única
  por usuario, destinatario y sesión, independiente de la versión del informe.
- No reintentar automáticamente reservas inciertas/fallidas: la recuperación
  cubre omisiones o fallos previos a la reserva, no garantiza entrega en bandeja.
- Sin cambios de estrategia, puntuaciones, umbrales, stops ni configuración.

## Pruebas

`python -m pytest tests/test_digest_schedule.py tests/test_alert_cli.py
tests/test_alert_runner.py -q`: **88 superadas**.

`python -m pytest -q`: **1280 superadas**, 1145 avisos de dependencias y
deprecaciones, en 79,97 segundos. Sin fallos. `git diff --check` y compilación
de los tres módulos Python modificados, correctos.

Cobertura: retrasos reales, cambio horario, límites de medianoche/06:00,
viernes/sábado, cambios de mes/año, reloj sin zona, ejecución manual, cotización
antigua, exclusión del día siguiente de Australia, conservación de `created_at`
y reejecución con informe distinto sin segundo correo. Las integraciones usan
SQLite temporal y remitente simulado: sin envío real, cambios en Supabase,
operaciones financieras ni publicación de datos privados.

La programación de GitHub es de mejor esfuerzo; si ambos arranques se omiten o
se retrasan hasta después de la ventana, no se promete un correo puntual.
Verificación en producción y primer resumen automático pendientes del despliegue.
