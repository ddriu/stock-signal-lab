# Validación del resumen diario — 26 de octubre de 2026

## Resultado

**Apto para publicar el código**, con verificación posterior de la versión
alojada. No se ha publicado esta versión ni enviado ningún correo real durante
la validación. Las pruebas no demuestran rentabilidad futura del modelo.

- Base Git: `5d3db55f61f089651c770e3ebf379e3fd1e11757`.
- Rama local: `codex/daily-score-digest-validation`.
- Suite final, después de todas las correcciones: **1.252 pruebas superadas**,
  0 fallos, 76,55 segundos.
- SQL: **70 comprobaciones superadas** en PostgreSQL local en memoria:
  20 de migraciones, 19 de importación conciliada, 26 del registro del modelo
  y 5 del informe diario. Sin conexión a una base de datos externa.
- Compilación de `app.py`, `src` y `scripts`: correcta.
- `git diff --check`: correcto.

## Funcionalidad comprobada

- Cartera antes que favoritas; cinco sesiones cerradas por mercado, con nombre
  de empresa, fecha, puntuación y comentario comprensible.
- Los datos ausentes quedan sin dato; no se reconstruyen puntuaciones pasadas
  ni se sustituyen por la puntuación de otro motor.
- Los cambios de versión, perfil, fuente o base contable no se presentan como
  cambios de mercado comparables.
- La antigüedad o inconsistencia de los fundamentales impide promocionar una
  compra sin datos verificables; no genera una señal de venta por sí misma.
- El correo compacto muestra como máximo cinco empresas y enlaza el mismo
  informe inmutable y versionado que se abre en la aplicación.
- Las rutas del historial y del enlace archivado no descargan cotizaciones ni
  recalculan o guardan análisis al abrirse. Comprobado con Streamlit AppTest.
- La programación usa las 23:00 de Europe/Madrid y contempla cambio horario,
  retrasos del programador y sesiones de mercados con festivos distintos.
- Una reserva atómica evita repetir un intento de correo para una misma
  sesión. Un resultado SMTP incierto no se recicla automáticamente.
- Los avisos omitidos del correo conservan su estado pendiente; no se marcan
  como notificados sin aparecer en él.

## Defectos encontrados y corregidos

1. Un envío manual intradía podía reintroducir señales de una sesión abierta
   dentro de un informe del cierre anterior. Ahora los avisos deben corresponder
   exactamente a una observación aceptada en el informe. La reproducción final
   registró cero llamadas al remitente, cero correos y cero escrituras de estado.
2. La referencia global al mercado estadounidense podía fechar mal el resumen
   europeo en un festivo de Estados Unidos. La fecha se obtiene ahora de las
   sesiones completadas del universo analizado; informe y reserva usan la misma
   sesión. Incluye prueba con almacenamiento SQLite real.

## Conservación y privacidad

Se comprobó que siguen sin cambios frente a la base Git `config.py`,
`src/indicators.py`, `src/signal_engine.py`, `src/backtesting.py`,
`src/growth_momentum.py`, `src/email_sender.py` y `src/storage.py`.
No se han retocado parámetros, puntuaciones ni cálculos contables del motor
original. Los controles nuevos califican la fiabilidad del dato y su presentación.

No se han modificado posiciones, operaciones, preferencias ni destinatarios.
La migración aditiva del resumen diario ya estaba instalada antes de esta
validación. En producción solo se consultaron metadatos: ambas tablas nuevas
tienen RLS y no permiten lectura a `anon` ni a `authenticated`. El acceso del
backend se filtra por propietario. Las pruebas verifican aislamiento entre
usuarios, integridad del informe, concurrencia y rechazo de reservas con fecha
o propietario incorrectos. Esto no constituye una auditoría de seguridad de
toda la aplicación.

## Reproducción

En un entorno con las dependencias del repositorio y pytest:

```sh
env EMAIL_SMTP_HOST= EMAIL_SMTP_USERNAME= EMAIL_SMTP_PASSWORD= SUPABASE_URL= SUPABASE_SECRET_KEY= python -m pytest -q --disable-warnings
python -m compileall -q app.py src scripts
node scripts/validate_migrations.mjs .
node scripts/validate_model_results_migration.mjs .
node scripts/validate_daily_digest_migration.mjs .
git diff --check
```

Los validadores SQL requieren `@electric-sql/pglite`. Entorno probado:
Python 3.12.14, pandas 2.3.3, NumPy 2.5.4, Streamlit 1.66.1,
yfinance 1.7.0, exchange_calendars 4.13.2 y PGlite 0.5.8 / PostgreSQL 18.3.

## Límites y comprobación posterior a publicar

- No se ha probado la recepción SMTP real ni ejecutado manualmente el envío
  en el alojamiento. Requiere autorización específica para un correo de prueba.
- La versión alojada sigue siendo la anterior hasta publicar. Después se debe
  comprobar el arranque, el acceso autenticado al resumen y a un informe propio,
  y el estado del workflow. GitHub Actions puede retrasar la hora de ejecución.
- Hay 1.135 avisos no bloqueantes en la suite, incluidos avisos de compatibilidad
  de NumPy/calendarios; no son fallos, pero merecen mantenimiento posterior.
- El historial solo mostrará puntuaciones realmente capturadas. Cinco sesiones
  de calendario no garantizan cinco observaciones disponibles por empresa.
- El registro garantiza como máximo un intento automático, no una entrega
  SMTP exactamente una vez. Los estados inciertos requieren revisión manual.
- Para revertir, restaurar el código previo y conservar las tablas e historiales;
  no borrar reservas ni reintentar correos inciertos sin revisión.
