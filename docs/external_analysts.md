# Analistas externos: revisión y activación

La ruta privada `/analistas`, accesible desde Radar y Más, incorpora recomendaciones recibidas legítimamente sin modificar la puntuación propia, los favoritos ni las operaciones reales. La entrega se prepara en una rama y una pull request; esta documentación no acredita una activación en producción.

## Importación y procedencia

- Se admiten correo `.eml`/`.msg` y CSV UTF-8 de hasta 2 MB, con previsualización antes de guardar. Los mensajes originales y los adjuntos permanecen fuera del repositorio y del almacenamiento estructurado.
- Se conserva proveedor, analista, fuente pública, identidad de la ficha, niveles explícitos y sus divisas, publicación, recepción y primera observación. Un dato ausente o ambiguo queda sin confirmar.
- El número anunciado en el asunto y el realmente extraído se muestran por separado. Las dos muestras privadas de desarrollo anunciaron 71 y 18 sugerencias, respectivamente; cada una contenía diez fichas detalladas. Los enlaces a las sugerencias restantes no se recorren automáticamente.
- Las recomendaciones que ya llegaron con objetivo o stop alcanzado son historial. No generan ganancias retrospectivas. Las entradas activadas por el proveedor antes de recibirlas tampoco acreditan una compra del sistema.
- Una importación manual no acredita autenticación del remitente. La lectura automática exige remitente exacto y autenticación DKIM alineada validada por Gmail.
- La fuente principal es el correo recibido. Los enlaces públicos permiten contrastar la ficha manualmente; no hay extracción masiva ni uso de endpoints privados de Perito.

Los tests usan exclusivamente empresas, mensajes y niveles sintéticos. Las muestras reales se validan fuera de Git.

## Qué muestra la pestaña

La tabla permite filtrar por analista, empresa, estado, fecha, favoritos y posiciones. Las fichas muestran entrada, stop, objetivo, riesgo, potencial y fuente. La lectura histórica de Stock Signal Lab usa únicamente análisis guardados que ya estaban disponibles al conocer la recomendación; si no existen se muestra N/D.

Supabase conserva por propietario las observaciones, revisiones, eventos, preferencias, reservas de correo y simulaciones. También existe un adaptador SQLite para uso local. Las funciones de servidor tienen acceso restringido; el navegador no recibe claves de servicio.

## Cuatro estrategias de 100 EUR

La comparación se activa expresamente desde la pestaña. Cada estrategia tiene 100 EUR virtuales y las mismas reglas de costes:

| Estrategia | Evidencia requerida |
| --- | --- |
| A · Resultados empresariales | Fuente y fecha contemporáneas del resultado |
| B · Beneficiarios indirectos | Fuente, fecha y relación causal documentada |
| C · Rebote o ruptura | Identificador de un análisis técnico propio guardado y su hora original |
| D · Analistas externos | Primera observación de la recomendación importada |

A, B y C admiten candidatos documentados; no se fabrican noticias ni señales para mantener las carteras invertidas. Las oportunidades sin evidencia permanecen a la espera. Las observaciones anteriores a la activación no se convierten en un backtest ganador.

Solo se usan sesiones y cotizaciones disponibles después de conocer la señal, con comisiones, spread, deslizamiento y cambio de divisa documentados. Las observaciones de mercado ya utilizadas se conservan para que una revisión posterior del proveedor no reescriba operaciones. Si stop y objetivo coinciden en una vela sin orden intradía verificable, se aplica la política conservadora documentada y se identifica la ambigüedad. Se muestra cobertura y número de operaciones junto a las métricas.

## Cuenta de correo existente

La aplicación ya utiliza Gmail mediante `src/email_sender.py`. Se reutilizan `EMAIL_SMTP_USERNAME`, `EMAIL_SMTP_PASSWORD` y `EMAIL_SENDER`; no se crea otra cuenta ni se cambia el destinatario guardado en las preferencias actuales.

La recepción preparada utiliza IMAP TLS en `imap.gmail.com:993`, carpeta en solo lectura y `BODY.PEEK`: no marca mensajes como leídos ni los borra. Que SMTP funcione no prueba que la lectura IMAP esté autorizada. La conexión real de lectura aún debe comprobarse al activarla; si Gmail la rechaza habrá que autorizar IMAP o la contraseña de aplicación en esa misma cuenta.

## Pasos para habilitarlo después de revisar la PR

1. Revisar y fusionar los cambios cuando se aprueben. Aplicar la migración aditiva `supabase/migrations/20261112082233_external_analysts.sql` con el procedimiento de despliegue del proyecto y comprobar las pruebas de permisos. Esta entrega no aplica esa migración a producción.
2. Abrir `/analistas`, importar y revisar una muestra, y confirmar que se guarda únicamente en la cuenta propietaria.
3. Asegurar que los correos del proveedor llegan a la cuenta Gmail existente. La muestra real identifica `avisos@mail.peritoinversor.com`; revisar una cabecera legítima antes de configurar un remitente diferente. No autorizar dominios completos ni el remitente de una prueba reenviada como si fuera el proveedor real.
4. En las variables del repositorio, configurar `EXTERNAL_ANALYSTS_MAILBOX_OWNER` con el propietario privado de la aplicación, `PERITO_ALLOWED_SENDERS` con el remitente exacto validado y `EXTERNAL_ANALYSTS_IMAP_FOLDER=INBOX` o la carpeta autorizada.
5. Para habilitar recepción, establecer `EXTERNAL_ANALYSTS_ENABLED=true` y `EXTERNAL_ANALYSTS_IMAP_ENABLED=true`. Se reutilizan los secretos de Supabase y SMTP existentes; no deben copiarse al código, a los correos ni al chat.
6. Si se desea el seguimiento en papel, activar expresamente la comparación en la pestaña. Sin activación no se crea una temporada ni se descargan cotizaciones para ella.
7. Para recibir el nuevo resumen, activar la preferencia independiente de analistas externos en la pestaña y `EXTERNAL_ANALYSTS_SEND_ENABLED=true` en las variables del repositorio. El destinatario sigue siendo el ya configurado. Conservar estas opciones desactivadas permite usar solo la consulta/importación manual.
8. Revisar la ejecución de «Analistas externos (opt-in privado)». La observación se programa cada hora laborable; el resumen solo se intenta entre 09:30 y 09:59 de Europe/Madrid y cuando hay cambios nuevos. GitHub puede retrasar el arranque. Los reintentos conservan las reservas para evitar duplicados; una entrega incierta requiere comprobar el buzón antes de recuperarla.

El nuevo resumen incluye hasta cinco cambios relevantes y enlaza a su ficha privada. Sus preferencias y registros son independientes de los correos actuales de Stock Signal Lab y de la Arena. La aceptación SMTP acredita que el servidor aceptó el mensaje, no su llegada a la bandeja de entrada.

## Validación

Las pruebas cubren importación y duplicados, aislamiento por propietario, historial inmutable, reserva de correo, errores de transporte, disponibilidad temporal, revisiones y costes. El script `scripts/validate_external_analysts_migration.mjs` usa PostgreSQL WASM temporal y no se conecta a producción. La suite de Streamlit usa usuarios sintéticos y bloquea los transportes reales.
