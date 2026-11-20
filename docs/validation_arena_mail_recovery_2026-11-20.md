# Recuperación del correo de Arena — 20/11/2026

## Reproducción sintética

La revisión nocturna puede quedar omitida por un inicio tardío aunque todavía exista una ventana válida para enviar las decisiones de la siguiente sesión. La recuperación de Arena debe comprobar el calendario bursátil por separado, sin recalcular el resumen ni duplicar SMTP.

## Cambio

- Arena puede recuperar un informe ya guardado después de la ventana nocturna de Stock; su límite sigue siendo la apertura real de la sesión siguiente, comprobada con el calendario XNYS.
- Dos intentos matinales exclusivos de Arena (07:35 y 10:35, Europe/Madrid); no recalculan señales ni envían el resumen de Stock.
- Un único reintento adicional para fallos recuperables de preparación. Se conservan las respuestas válidas. Nunca se reintenta SMTP ni una reserva incierta.
- Códigos de error acotados sin cuerpos privados y estado explícito `sent`, `already_accepted`, `pending` u `omitted_*` en el resumen del job.
- Se conservan reservas, destinatarios, posiciones y reglas. No se cambian datos históricos para forzar una recuperación.

## Pruebas

- Dos regresiones fallaron sobre el código anterior: recuperación después de las 06:00 y bloqueo del workflow por Stock omitido.
- 114 pruebas dirigidas pasaron con servicios sintéticos, sin red ni SMTP real: Arena, CLI de alertas y horarios.
- Batería completa local: 2.206 pruebas pasan. Sintaxis Python, YAML y diff sin errores.
- Casos añadidos: preparación parcial, reintento limitado, cruce de apertura, reserva tras timeout, correo ya aceptado, prioridades de errores y exclusión de fallos no recuperables.
- Un resultado verde por omisión no se considera una entrega. Los diagnósticos y registros de entrega privados no se publican en el repositorio.
