# Hoy: actualización y métodos en la misma pantalla

- **Actualizar revisión** inicia una consulta explícita de precios y fundamentales.
  El análisis se muestra en Hoy, sin enviar al usuario al Radar.
- La revisión manual se conserva sólo en esa sesión y para su propietario. No
  se guardan nuevas observaciones ni informes de correo desde esta acción,
  ni se reservan entregas, actualizan avisos o ejecutan simulaciones.
- Los cierres incompletos no se presentan como cierres nuevos. Si falla la
  consulta, o sólo devuelve cierres anteriores, se conserva la última lectura.
- **Técnico / Crecimiento / Tesis** cambia la lectura, no el intervalo de
  cotización. El contexto técnico y de tesis se enlaza a la sesión y a la
  observación exacta mediante una consulta acotada; cambiar de lectura no
  descarga mercado ni recalcula señales.
- Se distingue información ausente de una lectura disponible que no confirma
  entradas. Calidad y valoración sin tesis se presentan como investigación.
- Los enlaces `?digest=` siguen abriendo el informe original. **Volver a Hoy**
  sale del archivo sin lanzar descargas o envíos.

No se cambian reglas de inversión, posiciones, destinatarios o horarios. Las
pruebas usan observaciones sintéticas y no acreditan la latencia del proveedor
ni la recepción de correo en producción.
