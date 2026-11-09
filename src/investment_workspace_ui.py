"""Small, read-only investment workspace; navigation never places an order."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from math import isfinite
from zoneinfo import ZoneInfo

import streamlit as st

from src.investment_workspace import build_investment_workspace


HORIZON_LABELS = {"days": "Días", "weeks": "Semanas", "long_term": "Largo plazo"}
HOME_READING_LABELS = {
    "days": "Técnico",
    "weeks": "Crecimiento",
    "long_term": "Tesis",
}
WORKSPACE_UI_VERSION = "investment-workspace-ui-v3"


def _load_workspace(journal, report, snapshots, holdings, reference_date):
    # A provided report (including {}) is authoritative: no additional reads.
    if report is None:
        reader = getattr(journal, "get_latest_daily_score_report", None)
        if callable(reader):
            report = reader()
    report = report if isinstance(report, Mapping) else {}
    if holdings is None:
        holdings = [row for row in report.get("rows", [])
                    if isinstance(row, Mapping) and row.get("held")]
    return build_investment_workspace(report, holdings=holdings,
        snapshots=snapshots, reference_date=reference_date or datetime.now(ZoneInfo("Europe/Madrid")))


def _navigate(on_action, name, ticker=None):
    if callable(on_action):
        on_action(name, ticker)


def _note(value):
    if value is None or isinstance(value, bool):
        return "N/D"
    try:
        number = float(value)
        return f"{number:g}/100" if isfinite(number) else "N/D"
    except (TypeError, ValueError, OverflowError):
        return "N/D"


def _saved_growth_note(row):
    """A compact archived reading, separate from the position's own strategy."""
    reading = row.get("saved_growth_reading") or {}
    comparison = reading.get("comparison_status")
    change = reading.get("change")
    delta = f"{change:+g}" if comparison in {"stable", "changed"} and change is not None else (
        "no comparable" if comparison == "not_comparable" else "—")
    session = reading.get("as_of")
    try:
        session = datetime.fromisoformat(session).strftime("%d/%m")
    except (TypeError, ValueError):
        session = "sin fecha"
    text = f"{_note(reading.get('score'))} · {delta} · {session}"
    return text if reading.get("verified") else text + " · sin verificar"


def _header(workspace):
    st.subheader("Hoy · cartera y posibles entradas")
    st.write("Primero lo que ya tienes. Después, lecturas independientes de posibles entradas.")
    st.caption(f"Cierre guardado: {workspace.get('as_of') or 'sin fecha'} · "
        f"Informe: {str(workspace.get('report_id') or 'N/D')[:12]}. "
        "Abrir esta página no descarga precios, envía correos ni ejecuta operaciones.")
    st.caption("Las notas son lecturas del modelo, no porcentajes de beneficio. Revisa precio, costes y riesgo antes de actuar.")
    coverage = workspace.get("coverage") or {}
    if workspace.get("status") == "unavailable":
        if workspace.get("report_id"):
            st.warning(
                "El informe no contiene notas recientes de crecimiento suficientes. "
                "Las lecturas técnicas o de tesis enlazadas pueden seguir visibles; "
                "N/D significa contexto pendiente, no cero oportunidades."
            )
        else:
            st.warning(
                "Falta un informe sellado y fechado para esta revisión. N/D significa "
                "contexto pendiente, no cero oportunidades."
            )
    elif workspace.get("status") == "partial":
        st.warning(f"Revisión parcial: {coverage.get('current_with_score', 0)}/{coverage.get('total', 0)} "
            "empresas con nota reciente. Las pendientes no son señales de venta.")


def _card(row, on_action, key):
    with st.container(border=True):
        name = row.get("company_name") or row.get("ticker") or "Empresa sin identificar"
        ticker = str(row.get("ticker") or "")
        st.text(f"{name} ({ticker})")
        st.write(str(row.get("action") or "Revisar datos"))
        st.write(str(row.get("reason") or "Falta una explicación guardada para esta decisión."))
        kind = str(row.get("score_kind") or "Nota del enfoque")
        note = kind if kind == "Sin nota conjunta" else kind + " " + _note(row.get("score"))
        st.caption(f"{row.get('strategy') or 'Estrategia pendiente'} · "
            f"{row.get('horizon') or 'Plazo sin definir'} · {note}")
        if row.get("held") and row.get("saved_growth_reading"):
            st.caption("Crecimiento + momentum guardado: " + _saved_growth_note(row) +
                ". Lectura independiente del plan y de la nota de calidad; no decide una salida.")
        components = [("Calidad", "quality_score"), ("Valoración", "valuation_score"),
                      ("Riesgo controlado", "risk_score")]
        observed_components = [label + " " + _note(row[field]) for label, field in components
                               if row.get(field) is not None]
        if observed_components:
            st.caption(" · ".join(observed_components))
        st.caption(f"Dato: {row.get('as_of') or 'sin fecha'} · "
            f"Fuente: {row.get('source') or 'no documentada'}")
        if row.get("next_step"):
            st.write("Siguiente paso: " + str(row["next_step"]))
        if callable(on_action):
            buttons = st.columns(2) if row.get("held") and row.get("plan_status") in {"missing", "unverified"} else [st]
            buttons[0].button("Analizar empresa", key=key, on_click=_navigate,
                args=(on_action, "company", ticker), width="stretch",
                help="Solicita el análisis de esta empresa; esta acción sí puede consultar y calcular datos.")
            if len(buttons) == 2:
                buttons[1].button("Revisar plan", key=key + "_plan", on_click=_navigate,
                    args=(on_action, "plan", ticker), width="stretch")


def _one_more(rows, on_action, key):
    """One selected card, not hundreds of eagerly rendered expander rows."""
    options = list(range(len(rows)))
    if st.session_state.get(key + "_choice") not in options:
        st.session_state[key + "_choice"] = 0
    index = st.selectbox("Empresa que quieres revisar", options,
        format_func=lambda index: f"{rows[index].get('company_name') or rows[index].get('ticker')} ({rows[index].get('ticker')})",
        key=key + "_choice")
    _card(rows[index], on_action, key + "_company")


def _section(title, rows, on_action, key, *, empty):
    st.markdown("### " + title)
    if not rows:
        st.caption(empty)
        return
    for index, row in enumerate(rows[:5]):
        _card(row, on_action, f"{key}_{index}")
    if len(rows) > 5:
        with st.expander(f"Ver las otras {len(rows) - 5} empresas"):
            _one_more(rows[5:], on_action, key + "_more")


def _home_empty_context(horizon, view):
    """Explain missing evidence without presenting it as a rejected entry."""
    counts = view.get("counts") if isinstance(view, Mapping) else {}
    counts = counts if isinstance(counts, Mapping) else {}
    if horizon == "long_term" and view.get("research"):
        return (
            "Hay datos de calidad y valoración, pero falta una tesis documentada. "
            "Se muestran como empresas para estudiar."
        )
    try:
        observed = int(counts.get("observed") or 0)
    except (TypeError, ValueError, OverflowError):
        observed = 0
    if observed == 0:
        return {
            "days": (
                "Falta contexto técnico verificable para esta revisión. "
                "Actualiza los datos antes de interpretar una configuración."
            ),
            "weeks": (
                "Falta contexto de crecimiento y momentum verificable para esta "
                "revisión. Las empresas permanecen pendientes, no descartadas."
            ),
            "long_term": (
                "Falta contexto de tesis, calidad y valoración enlazado a esta "
                "revisión. La ausencia de evidencia no confirma una decisión."
            ),
        }.get(horizon, "Falta contexto verificable para esta revisión.")
    return {
        "days": (
            "La lectura técnica observada no confirma candidaturas en este plazo; "
            "no se rebajan sus reglas para rellenar la lista."
        ),
        "weeks": (
            "La lectura de crecimiento observada no confirma candidaturas en este "
            "plazo; no se mezclan otras notas para rellenar la lista."
        ),
        "long_term": (
            "La revisión de tesis no confirma candidaturas de largo plazo. Calidad, "
            "valoración y tesis se mantienen separadas de una compra."
        ),
    }.get(horizon, "La lectura observada no confirma candidaturas en este plazo.")


def _home_refresh_action(on_action):
    """Visible explicit refresh; merely browsing the screen stays read-only."""
    st.button(
        "Actualizar revisión",
        icon=":material/refresh:",
        type="primary",
        key="investment_home_refresh",
        on_click=_navigate,
        args=(on_action, "refresh", None),
        width="stretch",
        help=(
            "Solicita una revisión nueva. Abrir la pantalla o cambiar de etiqueta "
            "no consulta proveedores ni modifica posiciones."
        ),
    )


def _actions(on_action, *, prefix, include_refresh=True):
    if not callable(on_action):
        return
    actions = [("Mi cartera", "portfolio"), ("Resultados", "results")]
    if include_refresh:
        actions.append(("Revisar datos", "refresh"))
    for column, (label, action) in zip(st.columns(len(actions)), actions, strict=True):
        column.button(label, key=f"{prefix}_{action}", on_click=_navigate,
            args=(on_action, action, None), width="stretch")


def render_investment_home(journal, *, report=None, on_action=None,
        snapshots=None, holdings=None, reference_date=None):
    """One portfolio table and at most three candidates, without recalculation."""
    try:
        workspace = _load_workspace(journal, report, snapshots, holdings, reference_date)
    except Exception:
        st.error("No se pudo leer tu plan privado. No se han cambiado posiciones ni enviado correos.")
        return None
    _header(workspace)
    _home_refresh_action(on_action)
    horizon = st.radio("Lectura", list(HOME_READING_LABELS),
        format_func=HOME_READING_LABELS.get, horizontal=True, index=1,
        key="home_horizon_filter")
    st.caption(
        "Cambia la lectura de posibles entradas. No cambia el cierre ni recalcula "
        "señales. Técnico, Crecimiento y Tesis conservan reglas y fuentes "
        "independientes; no mezcla notas."
    )
    portfolio = workspace.get("portfolio") or []
    st.markdown("### Qué revisar primero")
    if portfolio:
        st.dataframe([{"Empresa": row.get("company_name") or row.get("ticker"),
            "Crecimiento": _saved_growth_note(row),
            "Qué revisar": row.get("action"),
            "Motivo": row.get("reason")} for row in portfolio], hide_index=True,
            width="stretch")
        st.caption("Crecimiento: nota guardada de crecimiento + momentum · cambio comparable · sesión. "
            "Es independiente del plan de cada posición; no es una nota de calidad ni una indicación de salida.")
        with st.expander("Detalle de una posición"):
            _one_more(portfolio, on_action, "home_position_detail")
    else:
        st.info("No hay posiciones incluidas en este informe. Puedes revisar las posibles entradas por plazo.")
    view = workspace.get("horizons", {}).get(horizon, {})
    _section("Posibles entradas · máximo tres", (view.get("opportunities") or [])[:3],
        on_action, "home_candidates", empty=_home_empty_context(horizon, view))
    research = view.get("research") or []
    if horizon == "long_term" and research:
        _section("Negocios para estudiar · no son compras confirmadas", research[:3],
            on_action, "home_long_term_research", empty="")
    _actions(on_action, prefix="investment_home", include_refresh=False)
    st.caption("Revisar cada día no obliga a operar cada día.")
    return workspace


def render_investment_workspace(journal, *, report=None, on_action=None,
        snapshots=None, holdings=None, reference_date=None):
    """One horizon at a time; portfolio precedes potential new purchases."""
    try:
        workspace = _load_workspace(journal, report, snapshots, holdings, reference_date)
    except Exception:
        st.error("No se pudo leer tu plan privado. No se han cambiado posiciones ni enviado correos.")
        return None
    _header(workspace)
    if st.session_state.get("investment_horizon") not in HORIZON_LABELS:
        st.session_state["investment_horizon"] = "weeks"
    horizon = st.radio("Plazo de la decisión", list(HORIZON_LABELS),
        format_func=HORIZON_LABELS.get, horizontal=True, key="investment_horizon")
    view = workspace.get("horizons", {}).get(horizon, {})
    st.write(view.get("description") or "Pendiente de revisión.")
    _section("Mi cartera en este plazo", view.get("portfolio") or [], on_action,
        f"investment_{horizon}_portfolio", empty="No hay posiciones con este plazo documentado.")
    unassigned = workspace.get("unassigned_portfolio") or []
    if unassigned:
        _section("Planes por verificar o definir", unassigned, on_action,
            f"investment_{horizon}_unassigned", empty="")
    _section("Posibles entradas", view.get("opportunities") or [], on_action,
        f"investment_{horizon}_opportunities",
        empty="No hay candidaturas confirmadas en los datos disponibles de este enfoque.")
    research = view.get("research") or []
    if horizon == "long_term" and research:
        _section("Negocios para estudiar · no son compras confirmadas", research, on_action,
            f"investment_{horizon}_research", empty="")
    waiting = view.get("waiting") or []
    if research:
        research_tickers = {row.get("ticker") for row in research}
        waiting = [row for row in waiting if row.get("ticker") not in research_tickers]
    if waiting:
        with st.expander(f"En espera o pendientes · {len(waiting)}"):
            _one_more(waiting, on_action, f"investment_{horizon}_waiting")
    with st.expander("Cómo leer este plazo"):
        for limitation in list(workspace.get("limitations") or []) + list(view.get("limitations") or []):
            st.write(str(limitation))
        st.caption("Cada estrategia mantiene sus reglas. Una mala entrada actual no obliga a vender una posición existente.")
    _actions(on_action, prefix="investment_workspace")
    return workspace
