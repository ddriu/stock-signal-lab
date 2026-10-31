"""Explicit activation/read actions for immutable virtual model tracking."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import importlib
import inspect
from io import BytesIO
import json
import os

import pandas as pd
import streamlit as st

from src.model_results_engine import ExecutionCosts
from src.model_results_service import public_frame, public_value
from src.model_results_store import create_model_results_store
import src.strategy_audit_data as _audit_data_module
import src.model_results_tracking as _tracking_module

if "cohort" not in inspect.signature(_audit_data_module.normalize_audit_signals).parameters:
    _audit_data_module = importlib.reload(_audit_data_module)
if (getattr(_tracking_module, "TRACKING_API_VERSION", "") != "measurement-readiness-v2"
        or _tracking_module.normalize_audit_signals is not _audit_data_module.normalize_audit_signals):
    _tracking_module = importlib.reload(_tracking_module)

from src.model_results_tracking import (TRACKING_VERSION, activate_model_experiment,
    advance_model_experiment, collect_model_market_bundle, experiment_readiness,
    resume_model_activation)
from src.strategy_audit_service import BENCHMARKS, audit_universe

TRACKING_PRESENTATION_VERSION = "measurement-readiness-v2"


def export_tracking_excel(seasons, snapshots, events):
    """Private in-memory literal-cell export; no formulas or external links."""
    from src.strategy_audit_engine import _safe
    from collections.abc import Mapping
    latest, daily, lots, indices, fills, archived, holds = {}, [], [], [], [], [], []
    for row in snapshots:
        sid, data = row["season_id"], row["snapshot"]
        latest[sid] = data
        daily.extend({"season_id": sid, **item} for item in data.get("daily", []))
    for sid, data in latest.items():
        lots.extend({"season_id": sid, **item} for item in data.get("lots", []))
        indices.extend({"season_id": sid, **item} for item in data.get("benchmarks", []))
        if data.get("matched_hold"):
            holds.append({"season_id": sid, **data["matched_hold"]["metrics"]})
    for event in events:
        if event["event_type"] == "order_filled":
            fills.append({"season_id": event["season_id"], "occurred_at": event["occurred_at"],
                "available_at": event["available_at"], "event_id": event["event_id"], **event["payload"]})
        # Excel has a cell text limit. Preserve complete evidence, including
        # calendars, as numbered chunks rather than silently truncating it.
        raw = json.dumps(_safe(public_value(event)), ensure_ascii=False, sort_keys=True, allow_nan=False)
        archived.extend(dict(event_id=event["event_id"], part=i // 30000 + 1,
            content=raw[i:i + 30000]) for i in range(0, len(raw), 30000))
    tables = {"Estadisticas": pd.DataFrame([dict(season_id=sid,
        coverage_complete=data.get("coverage_complete"), **(data.get("metrics") or {}))
        for sid, data in latest.items()]), "Transacciones": pd.DataFrame(fills),
        "Posiciones": pd.DataFrame(lots), "Evolucion": pd.DataFrame(daily),
        "Indices": pd.DataFrame(indices), "Misma seleccion mantener": pd.DataFrame(holds),
        "Configuracion": pd.DataFrame(seasons),
        "Eventos": pd.DataFrame(archived)}
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter", engine_kwargs={"options": {
            "strings_to_formulas": False, "strings_to_urls": False}}) as writer:
        for name, frame in tables.items():
            safe = public_frame(frame)
            for column in safe:
                safe[column] = safe[column].map(lambda value: json.dumps(_safe(value), ensure_ascii=False,
                    allow_nan=False) if isinstance(value, (Mapping, list, tuple)) else value)
            safe.to_excel(writer, sheet_name=name, index=False)
    return output.getvalue()


def render_model_tracking_page(journal):
    identity = str(journal.owner)
    key = "model_results_" + hashlib.sha256(identity.encode()).hexdigest()[:16]
    # Retrospective UI has its own hash helper. Read only that authenticated
    # account's state; never enumerate another account's cached report.
    from src.model_results_ui import _signature
    report_state = st.session_state.get("model_results_" + _signature(identity)[:16] + "_state", {})
    state = st.session_state.setdefault(key + "_tracking", {})
    st.markdown("### Seguimiento prospectivo · independiente de la cartera real")
    st.caption("Activar crea sólo ocho temporadas virtuales en USD. No compra acciones, no cambia tu cartera ni envía correos.")
    left, right = st.columns(2)
    if left.button("Consultar seguimiento guardado", key=key + "_read_tracking"):
        try:
            store = create_model_results_store(journal)
            state["seasons"] = [r for r in store.list_seasons() if r["manifest"].get("engine_version") == TRACKING_VERSION]
            state["snapshots"] = store.select("snapshots")
            state["events"] = store.select("events")
        except Exception:
            st.error("No se pudo consultar el registro. Comprueba que la migración del modelo esté instalada; no se cambiaron posiciones.")
    report = report_state.get("report")
    controls_changed = bool(report is not None and "current_run_key" in report_state
        and report_state.get("current_run_key") != report_state.get("run_key"))
    if report is not None:
        cohort_label = "verificada con los datos guardados entonces" if report.manifest.get("cohort", "raw") == "verified" else "motor bruto original"
        st.caption(f"Auditoría preparada para activar: {report.manifest.get('source', 'no documentada')} · {cohort_label}.")
    if controls_changed:
        st.warning("Los controles han cambiado desde la auditoría preparada. Recalcula antes de activar; no se usará un informe anterior como si correspondiera a la selección actual.")
    pending = state.get("pending_activation")
    pending_conflict = bool(report is not None and pending and (
        pending.get("audit_manifest") != report.manifest
        or pending.get("cohort", "raw") != report.manifest.get("cohort", "raw")))
    if pending_conflict:
        st.warning("Hay una activación pendiente con otra auditoría o cohorte; no se sustituirá su configuración. Consulta su registro o libera sólo el intento local para iniciar una activación independiente.")
        if st.button("Liberar sólo el intento local y conservar el registro anterior", key=key + "_release_pending"):
            state.pop("pending_activation", None)
            st.rerun()
    acknowledged = st.checkbox("He revisado la auditoría retrospectiva y sus limitaciones; quiero iniciar un experimento virtual nuevo.",
        key=key + "_confirm_activation")
    if right.button("Activar las cuatro estrategias virtuales", disabled=report is None or not acknowledged or pending_conflict or controls_changed,
                    key=key + "_activate_tracking"):
        try:
            # Freeze the actual report settings, not controls changed after the
            # report was calculated. Each activation is a separately named run.
            spec = report.budget["A"].manifest
            charges = ExecutionCosts(**spec["costs"])
            pending = state.setdefault("pending_activation", dict(
                now=pd.Timestamp.now(tz="UTC").isoformat(), audit_manifest=report.manifest,
                source=report.manifest["source"], cohort=report.manifest.get("cohort", "raw"),
                budget_usd=spec["budget_usd"], costs=asdict(charges)))
            # Keep exact activation time/settings across a partial RPC failure;
            # replay completes the same eight headers instead of creating more.
            experiment = activate_model_experiment(journal, pending["audit_manifest"], now=pending["now"],
                source=pending["source"], cohort=pending.get("cohort", "raw"),
                budget_usd=pending["budget_usd"], costs=ExecutionCosts(**pending["costs"]))
            snapshots = journal.list_analysis_snapshots()
            bundle = collect_model_market_bundle(sorted(set(audit_universe(snapshots)) | set(BENCHMARKS.values())),
                start=pd.Timestamp.now(tz="UTC").date().isoformat())
            advance_model_experiment(journal, experiment, snapshots, bundle.bars)
            state.clear()
            st.success("Temporadas virtuales creadas e inicializadas. Consulta el seguimiento para verlas.")
        except Exception:
            st.error("No se pudo completar la activación. Consulta el registro antes de reintentar: una cabecera ya creada se conserva y nunca se borra.")
    if os.getenv("MODEL_RESULTS_ENABLED", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        st.info("Esta sesión no acredita que el seguimiento de GitHub esté habilitado. La variable del alojamiento y la variable del programador son distintas; activar temporadas no acredita recogidas de mercado.")
    st.caption("Despliegue: aplicar la migración del registro y habilitar MODEL_RESULTS_ENABLED=true en las variables de GitHub. El proceso separado actualiza después de las señales y a las 22:30 UTC; no tiene credenciales SMTP.")
    seasons = state.get("seasons", [])
    if not seasons:
        st.caption("Consulta el registro para comprobar si hay temporadas activadas. La página no abre conexiones de seguimiento automáticamente.")
        return
    if st.button("Preparar Excel del seguimiento", key=key + "_tracking_excel"):
        state["excel"] = export_tracking_excel(seasons, state.get("snapshots", []), state.get("events", []))
    if state.get("excel"):
        st.download_button("Descargar seguimiento privado", state["excel"],
            file_name="seguimiento_modelo_original.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=key + "_tracking_download", on_click="ignore")
    choices = {r["season_id"]: r for r in seasons}
    groups = {}
    for row in seasons:
        groups.setdefault(row["manifest"]["experiment_id"], []).append(row)
    incomplete = [identifier for identifier, headers in groups.items() if len(headers) != 8]
    if incomplete:
        st.warning("Hay una activación interrumpida. Sus resultados no se calculan ni bloquean las temporadas completas.")
        pending_id = st.selectbox("Activación pendiente", incomplete, key=key + "_partial_activation")
        if st.button("Completar cabeceras pendientes sin duplicar", key=key + "_resume_activation"):
            try:
                resume_model_activation(journal, pending_id)
                st.success("Cabeceras reparadas con su configuración original. Consulta de nuevo el seguimiento.")
            except Exception:
                st.error("No se pudo completar: el motor debe coincidir con la versión congelada. No se han alterado operaciones.")
    selected = st.selectbox("Temporada virtual", list(choices), key=key + "_selected_tracking",
        format_func=lambda sid: " · ".join((choices[sid]["manifest"]["strategy_key"],
            "verificada entonces" if choices[sid]["manifest"].get("cohort", "raw") == "verified" else "motor bruto",
            choices[sid]["manifest"]["mode"], choices[sid]["manifest"]["activation_at"][:16])))
    saved = [r for r in state.get("snapshots", []) if r["season_id"] == selected]
    manifest = choices[selected]["manifest"]
    readiness = experiment_readiness(groups[manifest["experiment_id"]])
    if not readiness["ready"]:
        st.warning("El experimento no puede avanzar con esta versión o sus cabeceras están incompletas. Se conserva el histórico; consulta la configuración y crea una temporada separada si el motor ha cambiado.")
    initial = [row for row in saved if row["snapshot"].get("snapshot_kind") == "activation"]
    market = [row for row in saved if row["snapshot"].get("snapshot_kind") != "activation"]
    st.caption(f"Registros iniciales: {len(initial)} · Recogidas de mercado: {len(market)}. Son registros, no número de operaciones.")
    if not market:
        st.warning("Todavía no hay una recogida de mercado registrada para esta temporada. Las cabeceras y el efectivo inicial no demuestran rentabilidad ni funcionamiento diario.")
    with st.expander("Configuración congelada"):
        st.json(public_value(choices[selected]["manifest"]))
    if saved:
        data = saved[-1]["snapshot"]
        if not data.get("coverage_complete"):
            st.warning("Resultado todavía no concluyente: hay datos, ejecuciones pendientes o incidencias sin verificar. No se declara un ganador.")
        metrics = data.get("metrics")
        if data.get("snapshot_kind") == "activation":
            st.info("Sólo consta el efectivo virtual inicial. Rentabilidad: N/D, pendiente de la primera recogida de mercado; no se han simulado compras atrasadas.")
            st.dataframe(pd.DataFrame([{"Efectivo inicial USD": (metrics or {}).get("initial_cash_usd"),
                "Activación UTC": data.get("activation_at"), "Registro UTC": data.get("available_at")}]), hide_index=True)
        elif metrics:
            st.dataframe(pd.DataFrame([metrics]).drop(columns=["attribution_by_ticker"], errors="ignore"), hide_index=True)
            daily = pd.DataFrame([day for row in saved for day in row["snapshot"].get("daily", [])])
            if not daily.empty:
                st.line_chart(daily.drop_duplicates("date", keep="last").set_index("date")[["value_usd"]], y_label="Patrimonio USD")
            st.dataframe(public_frame(pd.DataFrame(data.get("benchmarks", []))), hide_index=True)
            if data.get("matched_hold"):
                st.markdown("#### Mantener exactamente las mismas compras")
                hold = data["matched_hold"]["metrics"]
                st.dataframe(pd.DataFrame([{name: hold.get(name) for name in (
                    "cohort_matches_model", "budget_feasible", "reference_requested_entries",
                    "reference_executed_entries", "capital_required_usd", "cash_shortfall_usd",
                    "final_value_usd", "net_return_pct", "coverage_complete")}]), hide_index=True)
                st.caption("Referencia con los mismos lotes y aportes, sin ventas. Si el capital no permite mantener esa selección completa, queda N/D: no se inventa financiación.")
            with st.expander("Posiciones y operaciones simuladas"):
                st.dataframe(public_frame(pd.DataFrame(data.get("lots", []))), hide_index=True)
                st.dataframe(public_frame(pd.DataFrame(data.get("trades", []))), hide_index=True)
        else:
            st.caption("El replay tiene operaciones sin una intención previa o revisiones del proveedor. Sus cifras quedan sólo como diagnóstico, no como rentabilidad prospectiva verificada.")
        st.caption("Última recogida UTC: " + str(data["available_at"]))
    feed = next((r for r in seasons if r["manifest"]["experiment_id"] == choices[selected]["manifest"]["experiment_id"]
        and r["manifest"]["mode"] == "theoretical" and r["manifest"]["strategy_key"] == "A"), None)
    events = [r for r in state.get("events", []) if r["season_id"] == selected or
        feed and r["season_id"] == feed["season_id"] and r["event_type"] in {"email_delivery", "incident"}]
    if any(event.get("payload", {}).get("code") == "collector_backlog_requires_new_season" for event in events):
        st.warning("Seguimiento pausado por varias sesiones no recogidas. No se convierten aportaciones atrasadas en compras actuales; requiere una nueva activación independiente.")
    with st.expander("Decisiones, órdenes, incidencias y evidencia de correo"):
        st.dataframe(public_frame(pd.DataFrame(events)), hide_index=True)
        st.caption("smtp_accepted confirma aceptación del servidor, no recepción en la bandeja. Sin recibo explícito sólo consta intento; una señal no acredita envío.")
