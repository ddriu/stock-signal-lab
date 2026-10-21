"""Explicit, owner-scoped retrospective model comparisons, without writes."""
from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
import hashlib
import json
import math

import pandas as pd
import streamlit as st

from src.model_results_engine import ExecutionCosts, FROZEN_MODELS
from src.model_results_service import (
    build_model_results, export_model_results_excel, public_frame, public_value,
)
from src.strategy_audit_data import normalize_audit_signals


_SOURCES = {"technical": "Técnica original", "growth": "Crecimiento + momentum"}


def _signature(value: object) -> str:
    return hashlib.sha256(json.dumps(value, default=str, sort_keys=True).encode()).hexdigest()


def _amount(value: object, percent: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return "N/D"
    return (f"{number:,.2f}" + ("%" if percent else "")) if math.isfinite(number) else "N/D"


def _render_report(report, prefix: str) -> None:
    st.caption(f"Fuente independiente: {_SOURCES[report.manifest['source']]} · "
               f"{report.manifest['start']} → {report.manifest['end']} · importes USD.")
    theoretical_tab, budget_tab, provenance_tab = st.tabs([
        "Teórico · aportaciones por entrada", "Presupuesto · efectivo limitado", "Referencia original y cobertura"])
    for tab, mode, models in ((theoretical_tab, "theoretical", report.theoretical),
                              (budget_tab, "budget", report.budget)):
        with tab:
            if mode == "theoretical":
                st.info("USD100 externos por cada compra ejecutada; sin límite de aportaciones ni costes. Las ventas se conservan como efectivo, sin financiar otras entradas.")
            else:
                st.info("Una sola aportación inicial; cada compra requiere USD100 más costes estimados. Las ventas netas conocidas pueden financiar compras posteriores. Sin recargas, compras parciales ni sobregiro.")
                st.caption("Se reserva la comisión fija de salida de cada lote no hold; esa reserva sigue siendo efectivo propio. SPY/QQQ/VT reciben sólo el mismo aporte inicial, no nuevos aportes por las ventas del modelo.")
            rows = []
            for model_id, result in models.items():
                m = result.metrics
                rows.append({"Modelo": model_id, "Regla": FROZEN_MODELS[model_id].name,
                    "Compras": m.get("executed_entries"), "Rechazos capital": m.get("budget_rejected_entries"),
                    "Aportaciones USD": m.get("contributions_usd"), "Patrimonio USD": m.get("final_value_usd"),
                    "Resultado neto USD": m.get("profit_usd"), "Retorno neto %": m.get("net_return_pct"),
                    "Realizado USD": m.get("realized_pnl_usd"), "Latente USD": m.get("unrealized_pnl_usd"),
                    "Efectivo USD": m.get("cash_usd"), "Costes USD": m.get("fees_usd"),
                    "Cobertura completa": m.get("coverage_complete")})
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            if any(not result.metrics.get("coverage_complete") for result in models.values()):
                st.warning("Hay cobertura incompleta o cohortes diferentes. N/D no significa cero y no se puede declarar un modelo ganador.")
            d = models["D"]
            if d.manifest.get("cohort_matches_A"):
                st.caption("D mantiene exactamente las entradas ejecutadas de A en esta modalidad; sin stops ni ventas.")
            else:
                st.warning("D no pudo financiar todas las entradas de A sin vender. A y D no son un contrafactual de cohorte idéntica en estos totales.")
                st.json(public_value({key: d.manifest.get(key) for key in (
                    "requested_A_event_ids", "missing_A_event_ids", "compatible_A_D_event_ids")}))
            import plotly.graph_objects as go

            chart = go.Figure()
            for model_id, result in models.items():
                if not result.daily.empty:
                    chart.add_scatter(x=result.daily.index, y=result.daily.value_usd,
                        name=model_id + " · patrimonio", mode="lines+markers", connectgaps=False)
            if chart.data:
                chart.update_layout(xaxis_title="Fecha UTC", yaxis_title="Patrimonio USD",
                                    margin=dict(l=0, r=0, t=15, b=0))
                st.plotly_chart(chart, width="stretch", key=prefix + "_curve_" + mode)
            st.caption("Curvas diarias: no se unen huecos de precios/FX; el TWR y drawdown publicados son aproximaciones, no medidas intradía exactas.")
            st.markdown("#### SPY / QQQ / VT con los mismos flujos externos")
            st.dataframe(public_frame(report.benchmarks.loc[report.benchmarks["mode"].eq(mode)]),
                         hide_index=True, width="stretch")
            st.markdown("#### Mantener exactamente los propios lotes de A / B / C")
            st.dataframe(public_frame(report.matched_hold_summary.loc[report.matched_hold_summary["mode"].eq(mode)]),
                         hide_index=True, width="stretch")
            st.caption("Cada fila mantiene las mismas cantidades, instantes e IDs de entrada de su propio modelo, con los mismos flujos externos y costes de compra. Si mantenerlos exige más capital que el presupuesto, la referencia queda N/D: no hay préstamo ni recarga. D continúa siendo la referencia de entradas de A.")
            model_id = st.selectbox("Detalle de modelo", list(models),
                format_func=lambda key: key + " · " + FROZEN_MODELS[key].name,
                key=prefix + "_detail_" + mode)
            selected = models[model_id]
            measures = {"TWR aproximado diario %": "twr_daily_approx_pct",
                "Drawdown aproximado diario %": "max_drawdown_daily_approx_pct",
                "XIRR anual % (si procede)": "xirr_pct", "Tasa de acierto %": "win_rate_pct",
                "Ratio ganancia/pérdida media": "gain_loss_ratio", "Profit factor": "profit_factor",
                "Rotación no anualizada %": "turnover_pct", "Duración media cerradas días": "average_duration_days",
                "Máximo capital invertido simultáneo USD": "max_simultaneous_invested_capital_usd"}
            st.dataframe(pd.DataFrame([dict(Medida=name, Resultado=_amount(selected.metrics.get(key)))
                                      for name, key in measures.items()]), hide_index=True, width="stretch")
            st.caption("XIRR: " + str(selected.metrics.get("xirr_reason", "N/D")))
            with st.expander("Atribución por activo"):
                st.dataframe(public_frame(pd.DataFrame(selected.metrics.get("attribution_by_ticker", []))), hide_index=True, width="stretch")
            with st.expander("Lotes y ventas simulados"):
                st.dataframe(public_frame(selected.lots), hide_index=True, width="stretch")
                st.dataframe(public_frame(selected.trades), hide_index=True, width="stretch")
            with st.expander("Evolución diaria, incidencias y manifiesto del modelo"):
                st.dataframe(public_frame(selected.daily.reset_index()), hide_index=True, width="stretch")
                st.dataframe(public_frame(selected.issues), hide_index=True, width="stretch")
                st.json(public_value(selected.manifest))
    with provenance_tab:
        st.warning("La referencia original está separada: conserva reglas y asignador originales. No comparte necesariamente los flujos, posiciones ni costes de los cuatro modelos y no se integra en su clasificación.")
        st.caption(str(report.manifest.get("original_reference_rule")))
        st.markdown("#### Referencia de salidas originales por lotes")
        st.dataframe(public_frame(pd.DataFrame([report.original_reference.metrics])), hide_index=True, width="stretch")
        st.markdown("#### Backtesting original por activo, sin modificar")
        st.dataframe(public_frame(report.original_backtest), hide_index=True, width="stretch")
        st.caption("Una ventana demasiado corta para el backtest original queda N/D, con motivo; no se rellena ejecutando un algoritmo diferente.")
        st.dataframe(public_frame(report.issues), hide_index=True, width="stretch")
        with st.expander("Observaciones válidas y registros apartados"):
            st.dataframe(public_frame(report.signals), hide_index=True, width="stretch")
            st.dataframe(public_frame(report.observations.rejected), hide_index=True, width="stretch")
        with st.expander("Reglas congeladas y procedencia completa"):
            st.json(public_value(report.manifest))


def render_model_results_page(journal, *, key_prefix: str = "model_results", git_sha: str = "unknown") -> None:
    """Opening/rerendering is inert; every read/fetch/calculation is explicit."""
    identity = str(getattr(journal, "owner", None) or getattr(journal, "path", None) or "authenticated-session")
    prefix = key_prefix + "_" + _signature(identity)[:16]
    state = st.session_state.setdefault(prefix + "_state", {})
    st.markdown("## Resultados de modelos originales")
    st.caption("Retrospectivo y sólo lectura: no opera, no modifica posiciones, no guarda simulaciones ni envía correos.")
    st.warning("Una señal guardada no demuestra un correo enviado ni una operación. Seguimiento prospectivo no activado en este panel.")
    st.caption("A: USD100 en cada Entrada fuerte, salir al Reducir/Vender. B: sólo nueva activación, salir al Vender. C: B con nota ≥85. D: entradas ejecutadas de A y mantener sin salidas. A/B/C conservan el stop original al cierre; no se añade trailing intradía.")
    if st.button("Cargar historial de modelos", key=prefix + "_load"):
        try:
            snapshots = journal.list_analysis_snapshots()
            if not isinstance(snapshots, pd.DataFrame):
                raise TypeError("Expected historical snapshots")
            state.clear()
            # Keep underlying signal metadata for normalization, but never emit
            # account columns or raw exception messages into the interface.
            state["snapshots"] = snapshots.copy(deep=True)
            for suffix in ("_start", "_end"):
                st.session_state.pop(prefix + suffix, None)
        except Exception:
            st.error("No se pudo cargar el historial; no se han cambiado datos.")
    if "snapshots" not in state:
        st.info("Carga el historial para empezar. Abrir la página no consulta la base ni descarga precios automáticamente.")
        return
    snapshots = state["snapshots"]
    if snapshots.empty:
        st.info("No hay observaciones históricas guardadas.")
        return
    source = st.selectbox("Fuente independiente de señales", list(_SOURCES), format_func=_SOURCES.get,
                          key=prefix + "_source")
    observed = normalize_audit_signals(snapshots, source=source)
    if observed.signals.empty:
        st.warning("Esta fuente no tiene observaciones válidas; no se sustituye por la otra ni se mezclan sus notas.")
        st.dataframe(public_frame(observed.rejected), hide_index=True, width="stretch")
        return
    last_day = pd.Timestamp.now(tz="UTC").date() - timedelta(days=1)
    first_day = min(observed.signals.observed_at.min().date(), last_day)
    left, right = st.columns(2)
    start = left.date_input("Desde (observación UTC)", value=first_day, min_value=first_day,
                           max_value=last_day, key=prefix + "_start")
    end = right.date_input("Hasta (día UTC completo)", value=last_day, min_value=first_day,
                          max_value=last_day, key=prefix + "_end")
    budget = st.number_input("Presupuesto virtual inicial USD", min_value=1.0, value=6000.0,
                             step=100.0, key=prefix + "_budget")
    left, middle, right = st.columns(3)
    commission = left.number_input("Comisión estimada USD por lado", min_value=0.0, value=1.0,
                                   step=.1, key=prefix + "_commission")
    spread = middle.number_input("Spread estimado bps por lado", min_value=0.0, max_value=9999.0,
                                 value=10.0, step=1.0, key=prefix + "_spread")
    fx = right.number_input("Coste FX estimado bps no USD", min_value=0.0, max_value=9999.0,
                            value=25.0, step=1.0, key=prefix + "_fx")
    charges = ExecutionCosts(commission, spread, fx)
    valid = start <= end and spread + fx < 10000
    if not valid:
        st.error("Revisa el orden de fechas y que spread + FX sea inferior a 10.000 bps.")
    market_key = _signature(dict(start=start, end=end))
    if st.button("Descargar precios de modelos e índices", disabled=not valid, key=prefix + "_prices"):
        try:
            from src.strategy_audit_service import BENCHMARKS, audit_universe, fetch_audit_market_bundle

            state.pop("bundle", None)
            with st.spinner("Descargando histórico, calendarios y FX; se conservarán las ausencias…"):
                universe = sorted(set(audit_universe(snapshots)) | set(BENCHMARKS.values()))
                bundle = fetch_audit_market_bundle(universe, start, end)
            state.update(bundle=bundle, market_key=market_key)
        except Exception:
            st.error("No se pudo descargar el histórico; no se ha modificado la cartera.")
    ready = state.get("bundle") is not None and state.get("market_key") == market_key
    if state.get("bundle") is not None and not ready:
        st.info("Las fechas cambiaron: descarga explícitamente el histórico del nuevo periodo.")
    run_key = _signature(dict(source=source, start=start, end=end, budget=budget,
        costs=asdict(charges), history=observed.manifest.get("input_sha256"),
        market=getattr(state.get("bundle"), "manifest", None)))
    if st.button("Calcular los cuatro modelos en ambas modalidades", disabled=not ready or not valid,
                 key=prefix + "_calculate"):
        try:
            bundle = state["bundle"]
            with st.spinner("Calculando cohortes, efectivo y referencias sin modificar datos…"):
                report = build_model_results(snapshots, bundle.bars, start=start.isoformat(),
                    end=end.isoformat(), source=source, budget_usd=budget, costs=charges,
                    git_sha=git_sha, market_manifest=bundle.manifest, market_issues=bundle.issues)
            state.update(report=report, run_key=run_key)
            state.pop("excel", None)
        except Exception:
            st.error("No se pudo completar el cálculo. Revisa cobertura y versión de caché; no hubo escrituras ni correos.")
    if state.get("report") is None:
        return
    if state.get("run_key") != run_key:
        st.warning("Controles o datos cambiados: se muestran los resultados del cálculo anterior hasta recalcular.")
    _render_report(state["report"], prefix)
    if st.button("Preparar Excel privado de modelos", key=prefix + "_excel"):
        try:
            state["excel"] = export_model_results_excel(state["report"])
        except Exception:
            st.error("No se pudo preparar el Excel; los resultados siguen en pantalla.")
    if state.get("excel"):
        st.download_button("Descargar Excel privado de modelos", state["excel"],
            file_name="resultados_modelos_originales.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=prefix + "_download", on_click="ignore")
    st.caption("Excel generado en memoria únicamente cuando lo solicitas; no se escribe en base de datos ni se envía por correo.")
