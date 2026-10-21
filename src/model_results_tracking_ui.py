"""Explicit activation/read actions for immutable virtual model tracking."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
from io import BytesIO
import json
import os

import pandas as pd
import streamlit as st

from src.model_results_engine import ExecutionCosts
from src.model_results_service import public_frame, public_value
from src.model_results_store import create_model_results_store
from src.model_results_tracking import (TRACKING_VERSION, activate_model_experiment,
    advance_model_experiment, collect_model_market_bundle, resume_model_activation)
from src.strategy_audit_service import BENCHMARKS, audit_universe


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
    acknowledged = st.checkbox("He revisado la auditoría retrospectiva y sus limitaciones; quiero iniciar un experimento virtual nuevo.",
        key=key + "_confirm_activation")
    if right.button("Activar las cuatro estrategias virtuales", disabled=report is None or not acknowledged,
                    key=key + "_activate_tracking"):
        try:
            # Freeze the actual report settings, not controls changed after the
            # report was calculated. Each activation is a separately named run.
            spec = report.budget["A"].manifest
            charges = ExecutionCosts(**spec["costs"])
            pending = state.setdefault("pending_activation", dict(
                now=pd.Timestamp.now(tz="UTC").isoformat(), audit_manifest=report.manifest,
                source=report.manifest["source"], budget_usd=spec["budget_usd"], costs=asdict(charges)))
            # Keep exact activation time/settings across a partial RPC failure;
            # replay completes the same eight headers instead of creating more.
            experiment = activate_model_experiment(journal, pending["audit_manifest"], now=pending["now"],
                source=pending["source"], budget_usd=pending["budget_usd"], costs=ExecutionCosts(**pending["costs"]))
            snapshots = journal.list_analysis_snapshots()
            bundle = collect_model_market_bundle(sorted(set(audit_universe(snapshots)) | set(BENCHMARKS.values())),
                start=pd.Timestamp.now(tz="UTC").date().isoformat())
            advance_model_experiment(journal, experiment, snapshots, bundle.bars)
            state.clear()
            st.success("Temporadas virtuales creadas e inicializadas. Consulta el seguimiento para verlas.")
        except Exception:
            st.error("No se pudo completar la activación. Consulta el registro antes de reintentar: una cabecera ya creada se conserva y nunca se borra.")
    if os.getenv("MODEL_RESULTS_ENABLED", "0").lower() not in {"1", "true"}:
        st.info("El seguimiento programado aún no está habilitado en este alojamiento. Activar una temporada no acredita que el programador de GitHub esté funcionando.")
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
            choices[sid]["manifest"]["mode"], choices[sid]["manifest"]["activation_at"][:16])))
    saved = [r for r in state.get("snapshots", []) if r["season_id"] == selected]
    with st.expander("Configuración congelada"):
        st.json(public_value(choices[selected]["manifest"]))
    if saved:
        data = saved[-1]["snapshot"]
        if not data.get("coverage_complete"):
            st.warning("Resultado todavía no concluyente: hay datos, ejecuciones pendientes o incidencias sin verificar. No se declara un ganador.")
        metrics = data.get("metrics")
        if metrics:
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
        feed and r["season_id"] == feed["season_id"] and r["event_type"] == "email_delivery"]
    with st.expander("Decisiones, órdenes, incidencias y evidencia de correo"):
        st.dataframe(public_frame(pd.DataFrame(events)), hide_index=True)
        st.caption("smtp_accepted confirma aceptación del servidor, no recepción en la bandeja. Sin recibo explícito sólo consta intento; una señal no acredita envío.")
