"""Small, read-only investment workspace; navigation never places an order."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from math import isfinite
from zoneinfo import ZoneInfo

import streamlit as st

from src.investment_workspace import build_investment_workspace


HORIZON_LABELS = {"days": "Días", "weeks": "Semanas", "long_term": "Largo plazo"}
WORKSPACE_UI_VERSION = "investment-workspace-ui-v1"
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


def _header(workspace):
    st.subheader("Tu plan de inversión")
    st.write("Primero lo que ya tienes. Después, posibles entradas según el plazo.")
    st.caption(f"Cierre guardado: {workspace.get('as_of') or 'sin fecha'} · "
        f"Informe: {str(workspace.get('report_id') or 'N/D')[:12]}. "
        "Abrir esta página no descarga precios, envía correos ni ejecuta operaciones.")
    st.caption("Las notas son lecturas del modelo, no porcentajes de beneficio. Revisa precio, costes y riesgo antes de actuar.")
    coverage = workspace.get("coverage") or {}
    if workspace.get("status") == "unavailable":
        st.warning("No hay una revisión utilizable. N/D significa sin información suficiente, no cero oportunidades.")
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


def _actions(on_action, *, prefix):
    if not callable(on_action):
        return
    for column, label, action in zip(st.columns(3),
            ("Mi cartera", "Resultados", "Revisar datos"),
            ("portfolio", "results", "refresh")):
        column.button(label, key=f"{prefix}_{action}", on_click=_navigate,
            args=(on_action, action, None), width="stretch")


def render_investment_home(journal, *, report=None, on_action=None,
        snapshots=None, holdings=None, reference_date=None):
    """Light overview: at most three priorities and three horizon entrances."""
    try:
        workspace = _load_workspace(journal, report, snapshots, holdings, reference_date)
    except Exception:
        st.error("No se pudo leer tu plan privado. No se han cambiado posiciones ni enviado correos.")
        return None
    _header(workspace)
    portfolio = workspace.get("portfolio") or []
    st.markdown("### Qué revisar primero")
    priorities = portfolio[:3]
    if portfolio and all(row.get("plan_status") == "unverified" for row in portfolio):
        st.info(f"Hay {len(portfolio)} posiciones cuyos planes no están incluidos en este resumen. "
            "Comprueba su plazo y tesis en la cartera antes de plantear cambios.")
        st.caption("No es una señal de venta ni significa que esas posiciones no tengan un plan guardado.")
        if callable(on_action):
            st.button("Revisar planes", key="investment_home_plans", on_click=_navigate,
                args=(on_action, "plan", None), width="stretch")
    elif priorities:
        for index, row in enumerate(priorities):
            _card(row, on_action, f"investment_home_priority_{index}")
    else:
        st.info("No hay posiciones incluidas en este informe. Puedes revisar las posibles entradas por plazo.")
    for column, horizon in zip(st.columns(3), HORIZON_LABELS):
        view = workspace.get("horizons", {}).get(horizon, {})
        with column:
            st.markdown("### " + HORIZON_LABELS[horizon])
            st.write(view.get("description") or "Pendiente de revisión.")
            label = view.get("opportunity_count_label", "N/D")
            st.caption(f"Posibilidades que revisar: {label}")
            if callable(on_action):
                st.button("Abrir " + HORIZON_LABELS[horizon].lower(), key=f"investment_home_{horizon}",
                    on_click=_navigate, args=(on_action, "horizon", horizon), width="stretch")
    _actions(on_action, prefix="investment_home")
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
