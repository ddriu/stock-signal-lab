"""Explicit, read-only Streamlit workflow for the recorded-signal audit."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import timedelta
import hashlib
import json
import math
import re
from types import SimpleNamespace

import pandas as pd
import streamlit as st

from config import StrategyConfig


_SOURCE_NAMES = {"technical": "Técnica original", "growth": "Crecimiento y momentum"}
_MODE_NAMES = {"daily": "Cada observación de entrada fuerte", "activation": "Sólo nuevas activaciones observadas"}
_SIZING_NAMES = {"fixed": "Importe fijo", "tiered": "Escalones por nota", "factors": "Escalones y factores históricos"}
_EXIT_NAMES = {
    "reduce": "Reducir o vender + stop al cierre del 8%",
    "sell": "Sólo vender + stop al cierre del 8%",
    "hold": "Mantener sin salidas",
    "confirmed": "Dos cierres bajo la media larga",
    "trailing10": "Trailing del 10% intradía",
    "fixed8": "Stop fijo del 8% intradía",
    "original": "Reglas originales: stop, trailing y señal",
}
_EMAIL = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+", re.I)
_PRIVATE_KEYS = {"owner", "email", "username", "recorded_by", "password", "secret", "token", "api_key"}


def _signature(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _private_key(name: object) -> bool:
    return str(name).casefold() in _PRIVATE_KEYS or "email" in str(name).casefold()


def _public_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _public_value(item) for key, item in value.items() if not _private_key(key)}
    if isinstance(value, (tuple, list)):
        return [_public_value(item) for item in value]
    if isinstance(value, str):
        return _EMAIL.sub("[correo oculto]", value)
    return value


def _public_frame(frame: pd.DataFrame) -> pd.DataFrame:
    visible = frame.drop(columns=[column for column in frame if _private_key(column)], errors="ignore").copy(deep=True)
    for column in visible:
        if visible[column].dtype == object or pd.api.types.is_string_dtype(visible[column]):
            visible[column] = visible[column].map(_public_value)
    return visible


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _amount(value: object, *, percent: bool = False) -> str:
    number = _number(value)
    return "N/D" if number is None else f"{number:,.2f}" + ("%" if percent else "")


def _cohort(lots: pd.DataFrame) -> list[tuple[str, str, float | None]]:
    return sorted((str(row.get("event_id")), str(row.get("entry_at")),
                   _number(row.get("contribution_usd"))) for row in lots.to_dict("records"))


def _comparison_row(name: str, report, *, same_lots: bool = True) -> dict[str, object]:
    complete = bool(report.metrics.get("coverage_complete"))
    interpretable = complete and same_lots and not report.lots.empty
    return {
        "Modelo": name, "Lotes ejecutados": len(report.lots),
        "Aportado USD": report.metrics.get("contributions_usd"),
        "Patrimonio final USD": report.metrics.get("final_value_usd") if interpretable else None,
        "Resultado USD": report.metrics.get("profit_usd") if interpretable else None,
        "Rentabilidad %": report.metrics.get("roi_pct") if interpretable else None,
        "Cobertura completa": complete, "Mismos lotes ejecutados": same_lots,
    }


def _render_results(state: dict[str, object], run_key: str, prefix: str) -> None:
    saved = state.get("results")
    if not saved:
        return
    report = saved["report"]
    context = saved["context"]
    st.markdown("### Último cálculo guardado")
    st.caption(
        f"{context['source_name']} · observaciones {context['start']} → {context['end']} · "
        f"{context['mode_name']} · {context['exit_name']}. Importes en USD."
    )
    if saved["run_key"] != run_key:
        st.info("Los controles o los datos han cambiado. Estos resultados son del cálculo anterior; vuelve a calcular para aplicar los cambios.")
    complete = bool(report.metrics.get("coverage_complete"))
    if report.lots.empty:
        st.warning("No se ejecutó ningún lote simulado. No hay una rentabilidad comparable.")
    elif not complete:
        st.warning("Cobertura insuficiente: no se puede comparar una rentabilidad total ni declarar una regla ganadora. Los precios, divisas o ejecuciones pendientes siguen como N/D.")
    else:
        st.info("Cobertura completa para este cálculo, no validación de una estrategia. El periodo y la muestra pueden ser cortos; no se elige una regla ganadora automáticamente.")
    a, b, c, d = st.columns(4)
    a.metric("Aportaciones ejecutadas (USD)", _amount(report.metrics.get("contributions_usd")))
    b.metric("Lotes simulados", len(report.lots))
    c.metric("Patrimonio final validado (USD)", _amount(report.metrics.get("final_value_usd")) if complete and not report.lots.empty else "N/D")
    d.metric("Rentabilidad total validada", _amount(report.metrics.get("roi_pct"), percent=True) if complete and not report.lots.empty else "N/D")
    st.caption("El patrimonio suma efectivo retirado por ventas y valor de lotes abiertos. Cada compra es una aportación externa; las ventas no financian otras compras.")
    partial_date = report.metrics.get("last_complete_valuation_date")
    partial_value = report.metrics.get("last_complete_value_usd")
    partial_contributions = report.metrics.get("last_complete_contributions_usd")
    partial_roi = report.metrics.get("last_complete_roi_pct")
    if partial_date is None and report.metrics.get("valuation_complete"):
        partial_date, partial_value = context["end"], report.metrics.get("final_value_usd")
        partial_contributions, partial_roi = report.metrics.get("contributions_usd"), report.metrics.get("roi_pct")
    if not complete and partial_date is not None and _number(partial_value) is not None and (_number(partial_contributions) or 0) > 0:
        with st.expander("Último corte parcial valorable", expanded=True):
            st.markdown(f"Corte valorable: **{partial_date}** · sólo las aportaciones ejecutadas hasta ese corte.")
            a, b, c = st.columns(3)
            a.metric("Aportado al corte parcial (USD)", _amount(partial_contributions))
            b.metric("Patrimonio al corte parcial (USD)", _amount(partial_value))
            c.metric("Rentabilidad del corte parcial", _amount(partial_roi, percent=True))
            st.caption(f"Resultado parcial, no rentabilidad de la estrategia completa ni prueba de una regla ganadora. No incluye entradas sin ejecutar. El corte indicado no se presenta como cierre de {context['end']} si falta esa valoración.")

    comparison, detail, coverage = st.tabs(["Comparaciones homogéneas", "Lotes y ventas", "Cobertura y procedencia"])
    with comparison:
        st.dataframe(_public_frame(saved["comparisons"]), hide_index=True, width="stretch")
        st.caption("Mantener compara los mismos lotes realmente ejecutados, con sus mismos instantes e importes de entrada; no una cartera distinta ni un desembolso inicial único.")
        if not saved["hold_matched"]:
            st.warning("La referencia de mantener no pudo reproducir exactamente las mismas entradas; no se considera una comparación homogénea.")
        st.markdown("**Índices con las mismas aportaciones**")
        st.dataframe(_public_frame(saved["benchmarks"]), hide_index=True, width="stretch")
        st.caption("SPY, QQQ y VT reciben las mismas aportaciones ejecutadas, en su primera apertura no anterior a cada aportación. Una referencia incompleta no se sustituye por cero.")
        st.caption("No se incluyen comisiones, impuestos, spread ni dividendos en efectivo. Stops intradía usan OHLC y FX de apertura como aproximación, no una cinta de ejecuciones real.")
        if not report.daily.empty:
            chart_columns = [name for name in ("value_usd", "contributions_usd") if name in report.daily]
            if chart_columns:
                import plotly.graph_objects as go

                chart = report.daily.loc[:, chart_columns].rename(columns={"value_usd": "Patrimonio USD", "contributions_usd": "Aportaciones USD"})
                figure = go.Figure()
                for column in chart:
                    figure.add_scatter(x=chart.index, y=chart[column], name=column,
                                       mode="lines+markers", connectgaps=False)
                figure.update_layout(xaxis_title="Sesión", yaxis_title="USD", margin=dict(l=0, r=0, t=15, b=0))
                st.plotly_chart(figure, width="stretch")
                st.caption("Sólo cortes observables; los huecos no se rellenan ni se unen con una rentabilidad inventada.")
    with detail:
        with st.expander("Resultados y riesgos de los lotes ejecutados"):
            labels = {
                "Capital recuperado por ventas (USD)": "withdrawals_usd",
                "Valor de posiciones abiertas (USD)": "open_value_usd",
                "Beneficio/pérdida realizado (USD)": "realized_pnl_usd",
                "Beneficio/pérdida latente (USD)": "unrealized_pnl_usd",
                "Tasa de acierto de ventas (%)": "win_rate_pct",
                "Duración media de posiciones cerradas (días)": "average_duration_days",
                "TWR aproximado diario (%)": "twr_daily_approx_pct",
                "Drawdown aproximado diario (%)": "max_drawdown_daily_approx_pct",
            }
            st.dataframe(pd.DataFrame([{"Medida": label, "Resultado": _amount(report.metrics.get(key))}
                                       for label, key in labels.items()]), hide_index=True, width="stretch")
            st.caption("Los resultados parciales no certifican toda la estrategia. El TWR y drawdown diarios son aproximaciones; no se calcula una XIRR anualizada en esta fase.")
        st.markdown("**Lotes simulados**")
        st.dataframe(_public_frame(report.lots), hide_index=True, width="stretch")
        st.markdown("**Ventas simuladas**")
        st.dataframe(_public_frame(report.trades), hide_index=True, width="stretch")
        with st.expander("Evolución diaria; N/D no significa cero"):
            st.dataframe(_public_frame(report.daily.reset_index()), hide_index=True, width="stretch")
        with st.expander("Diagnóstico posterior: sectores, salidas y seguimiento"):
            st.caption("Describe lo ocurrido después; no es información disponible para decidir la salida anterior. Los horizontes todavía no alcanzados permanecen pendientes.")
            for name, table in saved.get("diagnostics", {}).items():
                st.markdown(f"**{name}**")
                st.dataframe(_public_frame(table), hide_index=True, width="stretch")
            if not report.daily.empty:
                from src.strategy_audit_diagnostics import period_slices

                first, last = pd.Timestamp(context["start"]).date(), pd.Timestamp(context["end"]).date()
                window = st.date_input("Intervalo descriptivo: antes / durante / después", value=(first, last),
                    min_value=first, max_value=last, key=prefix + "_window_" + saved["run_key"][:8])
                if isinstance(window, (tuple, list)) and len(window) == 2:
                    st.dataframe(_public_frame(period_slices(report.daily.reset_index(), tuple(window))), hide_index=True, width="stretch")
                st.caption("La ventana la eliges tú. El historial completo sigue siendo la comparación principal; no se eliminan los días negativos.")
    with coverage:
        st.dataframe(_public_frame(saved["market_issues"]), hide_index=True, width="stretch")
        st.dataframe(_public_frame(report.issues), hide_index=True, width="stretch")
        rejected = saved["rejected"].drop(columns=["raw_record"], errors="ignore")
        st.markdown("**Registros apartados o revisiones posteriores**")
        st.dataframe(_public_frame(rejected), hide_index=True, width="stretch")
        st.json(_public_value(saved["input_manifest"]))
        st.caption("created_at es el reloj declarado de observación, generado por la aplicación; no es una certificación independiente ni prueba de envío de correos. Los precios descargados reconstruyen el histórico y pueden incorporar ajustes posteriores.")

    if st.button("Preparar Excel privado", key=prefix + "_excel_prepare"):
        try:
            from src.strategy_audit_service import audit_excel

            # The export uses sanitized copies and retains the daily date index.
            exported = SimpleNamespace(metrics=_public_value(dict(report.metrics)),
                daily=_public_frame(report.daily.reset_index()), lots=_public_frame(report.lots),
                trades=_public_frame(report.trades), issues=_public_frame(report.issues),
                manifest=_public_value(dict(report.manifest)))
            state["excel"] = audit_excel(exported,
                signals=_public_frame(saved["signals"]), rejected=_public_frame(rejected),
                comparisons=_public_frame(saved["comparisons"]), benchmarks=_public_frame(saved["benchmarks"]),
                forward=_public_frame(saved.get("diagnostics", {}).get("Seguimiento 5/20/60/120 sesiones", pd.DataFrame())),
                input_manifest=_public_value(saved["input_manifest"]))
        except Exception:
            st.error("No se pudo preparar el Excel. Los resultados siguen en pantalla; no se ha guardado ni enviado ningún archivo.")
    if state.get("excel"):
        st.download_button("Descargar Excel de auditoría", state["excel"],
            file_name=f"auditoria_estrategia_{context['start']}_{context['end']}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=prefix + "_excel_download", on_click="ignore")
    st.caption("El Excel se prepara en memoria y sólo se descarga si lo solicitas; no se escribe en la base de datos ni se envía por correo.")


def render_strategy_audit(journal) -> None:
    """Render an owner-scoped, explicit-read workflow without account mutations."""
    identity = str(getattr(journal, "owner", None) or getattr(journal, "path", None) or "current-authenticated-session")
    prefix = "strategy_audit_" + _signature(identity)[:16]
    state = st.session_state.setdefault(prefix + "_data", {})
    st.markdown("## Auditoría de señales históricas")
    st.caption("Sólo lectura: no realiza operaciones, no modifica posiciones ni análisis y no envía correos.")
    st.warning("Una señal guardada no demuestra que se enviara un correo. Se usa su fecha real de observación declarada (created_at), nunca se adelanta a la fecha de análisis.")
    if st.button("Cargar historial guardado", key=prefix + "_load"):
        try:
            snapshots = journal.list_analysis_snapshots()
            if not isinstance(snapshots, pd.DataFrame):
                raise TypeError("Expected snapshots dataframe")
            snapshots = snapshots.drop(columns=[column for column in snapshots if _private_key(column)], errors="ignore").copy(deep=True)
            state.clear()
            state["snapshots"] = snapshots
            for key in ("_start", "_end"):
                st.session_state.pop(prefix + key, None)
        except Exception:
            st.error("No se pudo leer el historial. Comprueba la conexión y vuelve a intentarlo; no se han cambiado datos.")
    if "snapshots" not in state:
        st.info("Primero carga el historial. Abrir esta página no consulta registros ni descarga precios automáticamente.")
        return
    snapshots = state["snapshots"]
    if snapshots.empty:
        st.info("No hay análisis históricos guardados para auditar.")
        return

    from src.strategy_audit_data import normalize_audit_signals

    source = st.selectbox("Fuente de las señales", list(_SOURCE_NAMES), format_func=_SOURCE_NAMES.get, key=prefix + "_source")
    observed = normalize_audit_signals(snapshots, source=source)
    bounds = observed.signals if source == "technical" else normalize_audit_signals(snapshots, source="technical").signals
    if bounds.empty:
        bounds = observed.signals
    st.caption(f"Historial cargado: {len(snapshots)} registros · {observed.coverage['historical_tickers']} activos históricos · {len(observed.signals)} observaciones válidas · {len(observed.rejected)} registros apartados. No se limita a las favoritas actuales.")
    if observed.signals.empty:
        st.warning("Esta fuente no contiene señales válidas con su procedencia y reloj de observación. No se rellenan con señales de otro motor.")
    if bounds.empty:
        st.dataframe(_public_frame(observed.rejected.drop(columns=["raw_record"], errors="ignore")), hide_index=True, width="stretch")
        return

    # A complete UTC day avoids treating today's partial daily candle as closed.
    last_day = pd.Timestamp.now(tz="UTC").date() - timedelta(days=1)
    first_day = min(bounds.observed_at.min().date(), last_day)
    a, b = st.columns(2)
    start = a.date_input("Desde (observación UTC)", value=first_day, min_value=first_day, max_value=last_day, key=prefix + "_start")
    end = b.date_input("Hasta (día UTC completo)", value=last_day, min_value=first_day, max_value=last_day, key=prefix + "_end")
    st.caption(f"Se seleccionan fechas de observación. Último día permitido: {last_day}; no se usa una vela de hoy todavía incompleta.")
    mode = st.selectbox("Cuándo aportar", list(_MODE_NAMES), format_func=_MODE_NAMES.get, key=prefix + "_mode")
    if mode == "activation":
        st.caption("Una activación necesita una observación previa no fuerte. El primer registro ya fuerte queda censurado, no se supone una activación.")
    a, b = st.columns(2)
    minimum = a.number_input("Nota mínima (0 = sin filtro)", min_value=0, max_value=100, value=0, step=1, key=prefix + "_minimum")
    amount = b.number_input("Aportación base por señal (USD)", min_value=.01, value=1.0, step=1.0, key=prefix + "_amount")
    sizing = st.selectbox("Tamaño de cada aportación", list(_SIZING_NAMES), format_func=_SIZING_NAMES.get, key=prefix + "_sizing")
    if sizing != "fixed":
        st.caption("Escalones: base ×1 para 75–79, ×2 para 80–84 y ×3 para 85–100. El modo factores multiplica por el mínimo de calidad, valoración, oportunidad y riesgo históricos; si faltan, no compra.")
    exit_policy = st.selectbox("Regla de salida", list(_EXIT_NAMES), format_func=_EXIT_NAMES.get, key=prefix + "_exit")
    variants = st.multiselect("Otras salidas para comparar", list(_EXIT_NAMES), default=[], format_func=_EXIT_NAMES.get, key=prefix + "_variants")
    st.caption("Las salidas se recalculan desde precios y lotes simulados; nunca se hereda la decisión de una posición real. «Reducir» cierra el lote completo en esta comparación.")
    valid_range = start <= end
    if not valid_range:
        st.error("La fecha inicial debe ser anterior o igual a la final.")
    selected = observed.signals
    if not selected.empty:
        selected = selected.loc[(selected.observed_at.dt.date >= start) & (selected.observed_at.dt.date <= end)]
    strong_count = int(selected.entry_label.eq("Entrada fuerte").sum())
    st.caption(f"Periodo seleccionado: {len(selected)} observaciones válidas; {strong_count} entradas fuertes antes de filtros y requisitos de ejecución.")
    market_key = _signature({"start": start, "end": end, "strategy": asdict(StrategyConfig())})
    if st.button("Descargar precios para esta auditoría", disabled=not valid_range or strong_count == 0, key=prefix + "_prices"):
        try:
            from src.strategy_audit_service import BENCHMARKS, audit_universe, fetch_audit_market_bundle

            state.pop("bundle", None)
            state.pop("bundle_key", None)
            with st.spinner("Descargando histórico y calendarios; las ausencias se conservan como incidencias…"):
                universe = sorted(set(audit_universe(snapshots)) | set(BENCHMARKS.values()))
                bundle = fetch_audit_market_bundle(universe, start, end, strategy_config=StrategyConfig())
            state.update(bundle=bundle, bundle_key=market_key, bundle_version=_signature(bundle.manifest))
        except Exception:
            st.error("No se pudo obtener el histórico. No se ha supuesto un precio ni se ha modificado la cartera.")
    ready = state.get("bundle") is not None and state.get("bundle_key") == market_key
    if state.get("bundle") is not None and not ready:
        st.info("El periodo cambió: descarga sus precios antes de calcular. El histórico anterior no se reutiliza para otro periodo.")
    config_values = dict(start=start.isoformat(), end=end.isoformat(), entry_mode=mode,
                         min_score=float(minimum) if minimum else None,
                         amount_usd=float(amount), sizing=sizing, exit_policy=exit_policy)
    run_key = _signature({"source": source, "config": config_values, "variants": variants,
                          "history": observed.manifest["input_sha256"], "market": state.get("bundle_version")})
    if st.button("Calcular auditoría con los datos descargados", disabled=not ready or not valid_range or strong_count == 0, key=prefix + "_calculate"):
        try:
            from src.strategy_audit_engine import AuditConfig, run_strategy_audit
            from src.strategy_audit_service import BENCHMARKS, matched_benchmark, prospective_results
            from src.strategy_audit_diagnostics import sector_results, exit_counterfactuals

            bundle = state["bundle"]
            config = AuditConfig(**config_values)
            report = run_strategy_audit(observed.signals, bundle.bars, config)
            executed = set(report.lots.event_id) if not report.lots.empty else set()
            hold_signals = observed.signals.loc[observed.signals.event_id.isin(executed)].copy()
            hold = run_strategy_audit(hold_signals, bundle.bars, replace(config, entry_mode="daily", exit_policy="hold"))
            hold_matched = _cohort(report.lots) == _cohort(hold.lots)
            rows = [_comparison_row("Salida elegida · " + _EXIT_NAMES[exit_policy], report),
                    _comparison_row("Mantener los mismos lotes ejecutados", hold, same_lots=hold_matched)]
            for alternative in variants:
                if alternative not in {exit_policy, "hold"}:
                    alternate_report = run_strategy_audit(observed.signals, bundle.bars, replace(config, exit_policy=alternative))
                    rows.append(_comparison_row(_EXIT_NAMES[alternative], alternate_report,
                                                same_lots=_cohort(report.lots) == _cohort(alternate_report.lots)))
            benchmarks = []
            for label, ticker in BENCHMARKS.items():
                result = matched_benchmark(report.lots, bundle.bars.get(ticker, pd.DataFrame()), end.isoformat())
                benchmarks.append({"Referencia": label, "Aportado USD": result.get("contributions_usd"),
                    "Valor mantenido USD": result.get("value_usd") if result.get("data_complete") else None,
                    "Rentabilidad %": result.get("roi_pct") if result.get("data_complete") else None,
                    "Cobertura completa": bool(result.get("data_complete")),
                    "Aportaciones sin ejecutar": result.get("pending_contributions")})
            context = dict(source_name=_SOURCE_NAMES[source], start=start.isoformat(), end=end.isoformat(),
                           mode_name=_MODE_NAMES[mode], exit_name=_EXIT_NAMES[exit_policy])
            state["results"] = dict(report=report, comparisons=pd.DataFrame(rows), benchmarks=pd.DataFrame(benchmarks),
                hold_matched=hold_matched, run_key=run_key, context=context,
                rejected=observed.rejected.copy(deep=True), signals=observed.signals.copy(deep=True),
                market_issues=bundle.issues.copy(deep=True),
                diagnostics={"Sectores históricos": sector_results(report.lots),
                    "Salir frente a mantener el mismo lote": exit_counterfactuals(report, hold, bundle.bars, end=end.isoformat()),
                    "Seguimiento 5/20/60/120 sesiones": prospective_results(selected, bundle.bars, end=end.isoformat())},
                input_manifest=dict(observations=observed.manifest, coverage=observed.coverage, market=bundle.manifest))
            state.pop("excel", None)
        except Exception:
            st.error("No se pudo completar la auditoría con esta configuración. Revisa la cobertura; no se ha ejecutado ninguna operación ni enviado correos.")
    _render_results(state, run_key, prefix)
