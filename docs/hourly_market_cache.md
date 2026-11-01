# Actualización horaria sin bloquear la navegación

## Dos procesos distintos

`hourly-market-cache.yml` recopila datos de transporte, no decisiones. Se
programa al minuto 17 de cada hora; los calendarios oficiales de cada mercado
deciden si hay trabajo. No necesita que la web esté abierta. No dispone de
secretos SMTP, no llama al motor ni escribe operaciones, fotografías de cartera,
análisis, informes o registros del laboratorio.

- Precios: como máximo una consulta por hora durante sesión y hasta 90 minutos
  después del cierre. Festivos, pausas y cambios de hora se respetan.
- Fundamentales: como máximo cada 24 horas, en lotes de 20 empresas, priorizando
  las ausentes y más antiguas. La preparación inicial puede cubrir todo el
  universo acotado de hasta 200 empresas; las ausentes no se convierten en ventas.
- Divisas: referencia diaria BCE, con fecha económica propia.
- Inicio: cabeceras y última cotización guardada, sin descargar históricos.
- Análisis: históricos comprimidos por instrumento y versiones fundamentales
  bajo demanda. Se conservan procedencia, unidades y atributos; no hay pickle.
- Una sesión abierta comprueba una cabecera ligera al cabo de una hora. Sólo
  reconstruye la vista si cambió la revisión; cambiar de pestaña no consulta
  proveedores ni reinicia el reloj horario.

La publicación inicial del workflow en `main` prepara la caché incluso fuera
del horario de mercado. Una modificación posterior de ese mismo workflow puede
repetir esa preparación. Para solicitarla manualmente existe `bootstrap=true`
en Actions. Nunca envía correo.
Las consultas manuales existentes siguen siendo explícitas e independientes.

## Precio indicativo no es una señal nueva

El precio horario es el último cierre de barra diaria que devuelve el proveedor
gratuito: puede ser parcial o demorado. Se muestra separado de la hora UTC de
consulta; no se inventa una hora exacta de cotización. Sólo las valoraciones
indicativas de cartera consumen esas cabeceras.

El análisis recibe velas de sesiones cerradas con 20 minutos de margen de
publicación. Etiquetas diarias ambiguas, mercados sin calendario, divisas
desconocidas y fechas futuras quedan pendientes, no se interpretan como ventas.
El laboratorio no recibe las valoraciones intradía. No cambian parámetros,
indicadores, stops ni puntuaciones de los motores congelados.

El histórico guardado cubre hasta cinco años más un pequeño margen, conservando
el periodo predeterminado. La interfaz recorta al rango seleccionado; para un
periodo más largo o precios sin ajustar hay que usar la actualización explícita.

## Persistencia y acceso

Migración: `supabase/migrations/20261031234204_hourly_market_cache.sql`.
La tabla `market_data_cache` es una caché mutable separada del registro inmutable
de medición. Su clave es propietario/tipo/instrumento. RLS está activada y los
roles de navegador no tienen permisos; sólo el backend utiliza su clave secreta.
La RPC es `SECURITY INVOKER` y rechaza observaciones más antiguas o futuras.

Ante un fallo no se sobrescribe la última observación válida. Las respuestas
parciales, corrupción, cambios de moneda y versiones incompatibles se rechazan.
Límites defensivos: 200 empresas por colector, 500 filas por lectura, 512 KiB por
payload y 32 MiB de descompresión. Alcanzar un límite se comunica como incidencia,
nunca como cobertura completa. No se publican listas privadas en logs ni Git.

## Instalación y validación

1. Aplicar la migración de forma no destructiva en el proyecto existente.
2. Publicar juntos el lector, el colector, la interfaz y el workflow.
3. Configurar `SUPABASE_URL` y `SUPABASE_SECRET_KEY` en Actions como hasta ahora;
   el propietario queda limitado al dueño del repositorio.
4. Comprobar la preparación inicial de despliegue y los contadores de caché. No se
   modifica ninguna preferencia de alertas ni se dispara `run_alerts`.

Las pruebas Python utilizan SQLite y proveedores sintéticos. La validación
`node scripts/validate_market_cache_migration.mjs .` ejecuta 23 comprobaciones
PostgreSQL en PGlite, incluida la falta de permisos públicos, CAS y rollback.
Las medianas de navegación poblada y sus límites están en
`docs/navigation_latency_2026-10-31.md`.

## Disponibilidad: límite que no resuelve esta caché

Streamlit Community Cloud duerme tras 12 horas sin tráfico. La caché conserva los
datos y el colector trabaja fuera de Streamlit, pero no garantiza una web siempre
encendida. No se instalan pings para eludir la suspensión. Una garantía 24/7
requiere otro alojamiento y una decisión explícita de servicio/presupuesto.

GitHub Actions puede retrasar u omitir ejecuciones programadas y, en repositorios
públicos, las desactiva tras 60 días sin actividad del repositorio. Por eso la
interfaz muestra la antigüedad real y no promete un SLA horario exacto.

Fuentes oficiales consultadas:

- [Hibernación de Streamlit](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app#app-hibernation).
- [Programación y límites de GitHub Actions](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
- [RLS de Supabase](https://supabase.com/docs/guides/database/postgres/row-level-security).
