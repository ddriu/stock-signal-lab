"""Small read-only results landing, separate from activation and diagnostics."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import math

import pandas as pd
import streamlit as st

from src.model_results_store import content_hash, create_model_results_store


def _navigate(callback, action):
    callback(action, None)


def _text(value):
    return str(value or "").strip()


def _group(header):
    manifest = header["manifest"]
    return tuple(_text(manifest.get(name)) or "no_documentado"
                 for name in ("experiment_id", "source", "cohort", "engine_version"))


def _group_label(group):
    experiment, source, cohort, _ = group
    source_label = {"growth": "Crecimiento + momentum", "technical": "Técnica original"}.get(source, "Enfoque no documentado")
    cohort_label = {"verified": "datos verificados entonces", "raw": "motor bruto"}.get(cohort, "cohorte no documentada")
    return f"{source_label} · {cohort_label} · experimento {hashlib.sha256(experiment.encode()).hexdigest()[:8]}"


def _checked_seasons(rows, owner):
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("Unbounded or invalid season response")
    for row in rows:
        if (not isinstance(row, Mapping) or row.get("owner") != owner
                or not isinstance(row.get("manifest"), Mapping)
                or not _text(row.get("season_id"))):
            raise ValueError("Invalid season ownership or format")
        if row.get("manifest_hash") != content_hash(row["manifest"]):
            raise ValueError("Invalid manifest seal")
    return rows


def _latest_snapshot(rows, owner, season_id, manifest):
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("Unbounded or invalid snapshot response")
    for row in rows:
        if (not isinstance(row, Mapping) or row.get("owner") != owner
                or row.get("season_id") != season_id
                or not isinstance(row.get("snapshot"), Mapping)):
            raise ValueError("Invalid snapshot ownership or format")
        if not isinstance(row.get("available_at"), str):
            raise ValueError("Missing snapshot timestamp")
        timestamp = pd.to_datetime(row["available_at"], utc=True, errors="coerce")
        if pd.isna(timestamp) or pd.Timestamp(row["available_at"]).tzinfo is None:
            raise ValueError("Missing snapshot timestamp")
        data = row["snapshot"]
        if row.get("snapshot_hash") != content_hash(data):
            raise ValueError("Invalid snapshot seal")
        for field in ("source", "cohort", "engine_version"):
            if field in data and data[field] != manifest.get(field):
                raise ValueError("Snapshot belongs to a different frozen selection")
    if not rows:
        return None
    # Follow ledger commit order, not an untrusted dict insertion order. New
    # providers may revise observations; availability and commit break ties.
    return max(rows, key=lambda row: (
        pd.Timestamp(row["available_at"]), int(row.get("commit_sequence") or 0),
        _text(row.get("run_id"))))


def _percentage(value):
    if value is None or isinstance(value, bool):
        return "N/D"
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return "N/D"
    return f"{number:+.2f}%" if math.isfinite(number) else "N/D"


def _summary(headers, latest, *, loaded):
    rows = []
    for strategy in "ABCD":
        matches = [row for row in headers if row["manifest"].get("strategy_key") == strategy]
        row = {"Estrategia": strategy, "Rentabilidad neta": "N/D", "Caída máxima aprox.": "N/D",
               "Última recogida UTC": "N/D", "Cobertura": "Sin consultar"}
        if len(matches) != 1:
            row["Cobertura"] = "Cabecera ausente" if not matches else "Cabeceras duplicadas"
        elif loaded:
            saved = latest.get(matches[0]["season_id"])
            data = saved["snapshot"] if saved else {}
            row["Última recogida UTC"] = _text((saved or {}).get("available_at")) or "N/D"
            metrics = data.get("metrics")
            if data.get("snapshot_kind") == "activation":
                row["Cobertura"] = "Sólo activación"
            elif not saved:
                row["Cobertura"] = "Sin recogida"
            elif (data.get("snapshot_kind") != "market_collection"
                    or data.get("coverage_complete") is not True or not isinstance(metrics, Mapping)
                    or metrics.get("coverage_complete") is False):
                row["Cobertura"] = "Incompleta / no verificada"
            else:
                row["Cobertura"] = "Completa según el registro"
                row["Rentabilidad neta"] = _percentage(metrics.get("net_return_pct"))
                row["Caída máxima aprox."] = _percentage(metrics.get("max_drawdown_daily_approx_pct"))
        rows.append(row)
    return pd.DataFrame(rows)


def render_investment_results(journal, *, on_action=None):
    """Opening/filtering is inert; only an explicit read opens the model store."""
    owner = _text(journal.owner)
    if not owner:
        st.warning("Inicia sesión para consultar tus resultados privados.")
        return
    key = "investment_results_v1_" + hashlib.sha256(owner.encode()).hexdigest()[:16]
    state = st.session_state.setdefault(key, {})
    st.subheader("¿Está funcionando el plan?")
    st.write("Separa tu resultado real del laboratorio virtual. Una nota alta no demuestra rentabilidad.")
    left, right = st.columns(2)
    with left:
        st.markdown("#### Tu cartera real")
        st.write("Consulta compras, ventas, comisiones y pérdidas o ganancias acumuladas en tu registro.")
        if callable(on_action):
            st.button("Ver mi rentabilidad real", key=key + "_real", on_click=_navigate,
                      args=(on_action, "real_returns"), width="stretch")
    with right:
        st.markdown("#### Laboratorio virtual A–D")
        st.write("Resultados guardados de cuatro reglas congeladas, sin operaciones reales.")
    clicked = st.button("Consultar seguimiento guardado", key=key + "_read")
    store = None
    if clicked:
        try:
            store = create_model_results_store(journal)
            if getattr(store, "owner", None) != owner:
                raise ValueError("Store owner mismatch")
            state["seasons"] = _checked_seasons(store.list_seasons(), owner)
            state["latest"] = {}
            state.pop("loaded_selection", None)
            state.pop("error", None)
        except Exception:
            state.clear()
            state["error"] = True
            store = None
    if state.get("error"):
        st.error("No se pudo verificar el seguimiento guardado. No se han cambiado posiciones, temporadas ni correos.")
    seasons = state.get("seasons") or []
    if not seasons:
        st.info("Aún no hay resultados consultados. Pulsa el botón para comprobar el registro; no se calculan ganancias ficticias.")
    else:
        groups = sorted({_group(row) for row in seasons})
        if st.session_state.get(key + "_experiment") not in groups:
            st.session_state[key + "_experiment"] = groups[0]
        selected = st.selectbox("Experimento y enfoque", groups, key=key + "_experiment", format_func=_group_label)
        headers = [row for row in seasons if _group(row) == selected]
        modes = sorted({_text(row["manifest"].get("mode")) or "no_documentado" for row in headers})
        if st.session_state.get(key + "_mode") not in modes:
            st.session_state[key + "_mode"] = modes[0]
        mode = st.selectbox("Modalidad", modes, key=key + "_mode", format_func=lambda value: {
            "theoretical": "Teórica", "budget": "Presupuesto virtual y costes configurados"}.get(value, "Modalidad no documentada"))
        headers = [row for row in headers if (_text(row["manifest"].get("mode")) or "no_documentado") == mode]
        selection = (selected, mode)
        if clicked and store is not None:
            try:
                latest = {}
                for strategy in "ABCD":
                    matching = [row for row in headers if row["manifest"].get("strategy_key") == strategy]
                    if len(matching) == 1:
                        season_id = matching[0]["season_id"]
                        latest[season_id] = _latest_snapshot(
                            store.select("snapshots", season_id=season_id), owner, season_id,
                            matching[0]["manifest"])
                state["latest"], state["loaded_selection"] = latest, selection
                state["read_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            except Exception:
                state.pop("loaded_selection", None)
                state["latest"] = {}
                st.error("No se pudieron verificar las recogidas de esta selección. Sus cifras quedan N/D.")
        loaded = state.get("loaded_selection") == selection
        if not loaded:
            st.info("Pulsa «Consultar seguimiento guardado» para leer esta selección. Cambiar filtros no abre conexiones.")
        st.dataframe(_summary(headers, state.get("latest") or {}, loaded=loaded), hide_index=True,
                     width="stretch")
        if loaded:
            st.caption("Consulta realizada UTC: " + state["read_at"])
        st.caption("A: compra cada sesión fuerte y sale al reducir. B: nueva activación fuerte y sale al vender. "
                   "C: nueva activación con nota ≥85 y sale al vender. D: las mismas compras que A, sin vender.")
    st.caption("N/D significa que falta medición fiable, no un 0%. El efectivo inicial y los registros de activación no cuentan como rentabilidad.")
    st.caption("No se declara un ganador ni se compara con tu cartera sin aportaciones, fechas, costes y divisa equivalentes. "
               "La caída máxima mostrada es una aproximación diaria, no una medida intradía.")
    if callable(on_action):
        st.button("Ver índices, operaciones y auditoría del modelo", key=key + "_details",
                  on_click=_navigate, args=(on_action, "model_details"), width="stretch")
