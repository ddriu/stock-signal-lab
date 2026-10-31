# Prioridades de medición y comparación de ejecución

Esta fase no altera el motor original, los indicadores, umbrales, stops,
asignación de USD100 ni reglas A–D. No activa experimentos, configura variables
remotas, publica código, envía correos ni registra operaciones reales.

## Seguimiento prospectivo

El workflow separado muestra el estado deshabilitado en vez de omitir el job
sin diagnóstico. Eso no habilita `MODEL_RESULTS_ENABLED` automáticamente. Antes
de descargar precios se comprueba compatibilidad; un motor distinto necesita una
activación explícita nueva. Las temporadas anteriores se conservan.

La activación guarda únicamente efectivo inicial, con rentabilidad N/D y reloj
actual. No confundirla con una recogida de mercado o un resultado del sistema.
Los reinicios son idempotentes. Un backlog de varias sesiones se registra y pausa
el experimento, sin acumular compras atrasadas ni ejecutarlas hacia atrás.

La configuración de GitHub y la del servidor Streamlit son distintas. La vista
privada explica qué está activado, qué se ha recogido y qué no puede avanzar.

## Cohortes independientes

- `technical/raw`: referencia técnica original.
- `growth/raw`: lectura bruta de crecimiento archivada, sin validar hoy su pasado.
- `growth/verified`: primera observación con verificación empresarial grabada
  entonces; falta de evidencia impide entrar pero conserva el estado observado.

La cohorte verificada no incorpora fundamentales actuales ni mejoras de una
revisión posterior de la misma sesión. Es una aproximación conservadora, no una
réplica de todas las oportunidades que una revisión posterior pudiera ofrecer.
Manifiestos, hashes y `raw_event_id` documentan esa separación. La referencia
original permanece bruta incluso al estudiar otra cohorte.

## Experimentos de ejecución

En **Analizar → Validar → Resultados del modelo**, tras cargar historial y
precios, el botón **Comparar filtros experimentales de ejecución** calcula:

1. Referencia intacta.
2. Contexto de liquidez normalizado a EUR, con datos de vela cerrada conocidos.
3. Lo anterior más un preset predeclarado de ruptura o retest.

Las tres variantes utilizan el mismo presupuesto, costes, precios y reglas A–D.
Conservan todas las observaciones: una entrada no admitida se enmascara, no se
borra. En B/C la activación pasa a ser la del estado efectivo filtrado, no se
presenta como otro cruce del motor bruto.

FX exige fecha económica y disponibilidad anterior a la observación; si falta
reloj, no se admite cambio del mismo día. GBp se normaliza una vez. Se respetan
los relojes de disponibilidad de OHLC si están presentes; en datos reconstruidos
sin ellos se declara el proxy de cierre de bolsa. El orden intradía de stop y
objetivo no se infiere a partir de una vela diaria.

Estos filtros no se activan en el motor diario ni en las temporadas originales.
El cálculo es explícito, privado y de lectura; abrir la página no descarga,
recalcula, escribe o envía nada. Un cambio de controles invalida su caché.

## Interpretación y validación

El efectivo cuenta como patrimonio. Menor drawdown por dejar casi todo en caja
no acredita mejor predicción. Valoración completa de las operaciones ejecutadas
y cobertura completa de las oportunidades son conceptos distintos; una cifra
calculable con cobertura parcial no basta para declarar ganador.

Los CSV, historiales y resultados de usuarios permanecen fuera del repositorio.
El histórico limitado, los precios reconstruidos y los fundamentales ausentes
no permiten afirmar mejoras fuera de muestra ni optimizar el preset con el mismo
periodo evaluado. Los dividendos y los impuestos siguen excluidos.

Validación de esta fase: **1.465 pruebas aprobadas**, `git diff --check` limpio y
sin modificaciones en archivos de motores, configuración e indicadores congelados.
