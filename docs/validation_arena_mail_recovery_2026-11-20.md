# Recuperación del correo de Arena — 20/11/2026

## Incidente observado (producción, solo lectura)

- Código publicado: `46e5957aa7eca5d9744c7e0873a51b425d088a99`.
- Run `44663483806`: Stock Signal Lab terminó; Arena respondió HTTP 409 al preparar.
- El registro privado confirmó el resumen de la sesión 19/11 como enviado.
- Tres modelos guardaron decisiones a las 02:56 UTC; el cuarto a las 08:42 UTC.
- Run `44695196094`: análisis omitido por inicio tardío; Arena se saltó por la condición del workflow. No fue una recuperación exitosa.
- No hay evidencia suficiente para atribuir el fallo original del cuarto modelo a créditos o a una validación concreta. Los registros antiguos solo conservan un código genérico.

## Cambio

- Arena puede recuperar un informe ya guardado después de la ventana nocturna de Stock; su límite sigue siendo la apertura real de la sesión siguiente, comprobada con el calendario XNYS.
- Dos intentos matinales exclusivos de Arena (07:35 y 10:35, Europe/Madrid); no recalculan señales ni envían el resumen de Stock.
- Un único reintento adicional para fallos recuperables de preparación. Se conservan las respuestas válidas. Nunca se reintenta SMTP ni una reserva incierta.
- Códigos de error acotados sin cuerpos privados y estado explícito `sent`, `already_accepted`, `pending` u `omitted_*` en el resumen del job.
- Se conservan reservas, destinatarios, posiciones y reglas. No se cambian datos históricos para forzar una recuperación.

## Pruebas

- Dos regresiones fallaron sobre el código anterior: recuperación después de las 06:00 y bloqueo del workflow por Stock omitido.
- 114 pruebas dirigidas pasaron con servicios sintéticos, sin red ni SMTP real: Arena, CLI de alertas y horarios.
- Casos añadidos: preparación parcial, reintento limitado, cruce de apertura, reserva tras timeout, correo ya aceptado, prioridades de errores y exclusión de fallos no recuperables.
- Las verificaciones reales de publicación y entrega se documentan en el PR y sus ejecuciones de Actions. Un resultado verde por omisión no se considera una entrega.
