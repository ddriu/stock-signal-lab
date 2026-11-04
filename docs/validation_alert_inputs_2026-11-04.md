# Corrección de cobertura y recuperación de alertas

## Problemas que reproduce esta revisión

1. Un intento programado de recuperación puede comenzar fuera de la ventana
   segura y confundirse con un fallo del transporte de correo.
2. Una descarga de histórico corto puede terminar antes que un histórico
   válido ya disponible en la caché privada. Mezclar mercados con diferentes
   últimas fechas puede dejar sin nota muchas filas del informe objetivo.
3. Un catálogo no analizado o parcialmente actualizado puede mostrar un cero
   que parece un resultado completo del motor.

## Corrección

- Un inicio programado fuera de horario se omite explícitamente, sin acceder
  a usuarios ni intentar correo o seguimiento. Se conserva la ventana de
  seguridad; los errores reales del proceso no se convierten en éxitos.
- Cada usuario puede aprovechar su propia caché de precios ya observados.
  Se selecciona una serie completa más reciente, no se concatenan proveedores
  ni unidades distintas. Se respetan fecha objetivo, cierre de mercado, hora
  original de observación y ventana de 550 días. Un empate conserva la
  descarga original. Una caché ausente o inválida no crea almacenamiento.
- Se aplican los mismos límites a las referencias generales y sectoriales.
  Las referencias atrasadas se identifican con su fecha, sin rellenar precios.
- Radar y crecimiento distinguen `N/D`, cobertura parcial y cero medido.
  El resumen de la página y el correo comparten una explicación de cobertura.
  Los informes archivados se presentan sin modificar su contenido ni sello.
- La compatibilidad del despliegue en caliente se comprueba antes de importar
  las nuevas funciones de presentación.

## Límites conservados

No se cambian estrategias, puntuaciones, medias, umbrales, stops, destinatarios
ni preferencias. No se reescribe el historial, no se reconstruyen órdenes
pasadas y no se reenvían correos registrados. No se garantiza que una revisión
completa produzca entradas fuertes; cero sigue siendo un resultado válido
cuando existen datos suficientes para medirlo.

## Validación reproducible

```bash
python -m pytest -q tests/test_alert_market_inputs.py \
  tests/test_digest_schedule.py tests/test_alert_runner.py \
  tests/test_alert_virtual_wiring.py tests/test_opportunity_unavailable_status.py \
  tests/test_daily_digest_partial_coverage.py tests/test_analysis_runtime_compat.py
python -m pytest -q
```

Las pruebas son sintéticas: no requieren acceso a datos privados, proveedores
ni SMTP. Una prueba integrada usa SQLite y la caché real de transporte con
una descarga anterior, una sesión objetivo cerrada y dos mercados. Comprueba
que se conservan las horas reales, se guarda la nota de la sesión correcta,
la referencia usa la fecha esperada y no se registran operaciones reales.

Las pruebas de interfaz cubren revisión ausente, parcial, completa, precios
antiguos, errores de análisis y conservación del histórico. El arranque público
en producción es una comprobación distinta de navegar dentro de una cuenta
autenticada y no demuestra entrega de correo en una bandeja de entrada.
