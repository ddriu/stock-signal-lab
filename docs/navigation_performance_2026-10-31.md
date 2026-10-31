# Navegación sin trabajo de mercado implícito

Esta mejora se limita a la interfaz y sus cachés de lectura por sesión. No
cambia parámetros, indicadores, puntuaciones, stops, correos ni reglas de los
experimentos. No instala un proceso horario ni modifica el alojamiento.

## Cambios

- Favoritos, ajustes de correo, guía, planificación y accesos salen del flujo
  general antes de preparar precios o recalcular el motor.
- Inicio y Cartera dejan de iniciar descargas al abrir una sesión. La
  actualización explícita conserva su botón, seguimiento por lotes y mensajes.
- El contexto de análisis se calcula una vez por revisión de datos,
  configuración, cartera y día de valoración. Se reutiliza entre páginas.
- Las lecturas del diario se reutilizan hasta 60 segundos dentro de la misma
  sesión, con claves separadas por propietario, conexión, método y argumentos.
  No es una caché privada global compartida entre usuarios.
- Una escritura o fallo ambiguo de escritura invalida las lecturas y vistas
  derivadas. Actualizar precios explícitamente descarta también esas lecturas.
- Al cambiar de usuario se eliminan el contexto de mercado, las vistas y los
  ajustes guardados del usuario anterior. Los ajustes del usuario actual se
  conservan al pasar por páginas que no dibujan la barra lateral.
- Las siete secciones de Cartera son de ejecución condicional: no se calculan
  resultados, evolución, formularios y comparaciones ocultas. Historial y
  Comprar/vender tienen rutas que evitan completamente el análisis de mercado.
- Las copias de vistas y lecturas aíslan también listas/diccionarios dentro de
  celdas de tablas; editar un resultado local no contamina la caché.

## Validación

Las pruebas son locales y no hacen consultas de mercado, operaciones reales,
escrituras a producción ni envíos de correo.

- Navegación real con Streamlit AppTest y funciones pesadas prohibidas.
- Inicio → repetición → Favoritos → Inicio: una preparación y un enriquecimiento,
  cero descargas. Actualizar explícitamente permite una ejecución nueva.
- Rutas ligeras sin cálculos, incluso con trabajo pendiente para otra sección.
- Cambio entre usuarios sin reutilizar precios o políticas del anterior.
- Conservación de controles al desaparecer y volver la barra lateral.
- Ejecución únicamente de la sección de Cartera seleccionada.
- Caducidad, invalidación por escritura, límites de memoria e independencia de
  copias en las cachés de lectura.
- Firma del experimento congelado de crecimiento verificado sin cambios:
  `8200fd930b8c8eab529917ea29bd922b32ecc791785f3c61cbe4f699b88f5f11`.
- Suite completa local final: 1.571 pruebas aprobadas en 122,54 segundos.
  En una ejecución anterior se observó un timeout de AppTest en el laboratorio
  con cobertura parcial; la prueba aislada y la repetición completa terminaron
  correctamente. No se han relajado sus aserciones ni su tiempo de espera.

No se promete latencia cero: renderizar una pantalla, el primer análisis, una
consulta que haya caducado o una actualización solicitada pueden tardar. El
objetivo es que navegar no repita trabajo pesado innecesario. Los tiempos de
producción y la reactivación del alojamiento gratuito no se han medido aquí.

Alcance de la entrega: navegación y cachés de lectura, validadas en local.
La actualización horaria en segundo plano y evitar la suspensión del alojamiento
son tareas distintas; esta revisión no las activa ni las da por resueltas.
La aprobación de CI y el arranque público se comprueban durante la publicación;
no equivalen a una prueba visual de una sesión privada en producción.
