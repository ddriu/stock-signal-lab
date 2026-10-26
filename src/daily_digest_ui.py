"""Simple private score history; opening this view never refreshes or sends."""
from __future__ import annotations

import re
from collections.abc import Mapping

import pandas as pd
import streamlit as st

from src.daily_digest import build_daily_score_report, seal_daily_score_report
from src.instruments import resolve_analysis_ticker, resolve_company_name
from src.portfolio_snapshot import latest_portfolio_snapshot


def _catalog(journal: object) -> tuple[set[str], set[str], dict[str, str]]:
    tickers, held, names = set(), set(), {}
    favorites = journal.list_favorites() if callable(getattr(journal, "list_favorites", None)) else pd.DataFrame()
    for row in favorites.to_dict("records"):
        raw = str(row.get("ticker") or "").strip()
        if raw:
            ticker = resolve_analysis_ticker(raw)
            tickers.add(ticker)
            names[ticker] = resolve_company_name(ticker, row)
    positions = journal.open_positions() if callable(getattr(journal, "open_positions", None)) else pd.DataFrame()
    for row in positions.to_dict("records"):
        raw = str(row.get("ticker") or "").strip()
        if not raw or "quantity" in row and (pd.isna(row["quantity"]) or float(row["quantity"]) <= 0):
            continue
        ticker = resolve_analysis_ticker(raw)
        held.add(ticker)
        names[ticker] = resolve_company_name(ticker, row, names.get(ticker))
    read = getattr(journal, "list_portfolio_snapshot_positions", None)
    if callable(read):
        latest, _ = latest_portfolio_snapshot(read())
        for row in latest.to_dict("records"):
            raw = str(row.get("analysis_ticker") or "").strip()
            if raw:
                ticker = resolve_analysis_ticker(raw)
                held.add(ticker)
                names[ticker] = resolve_company_name(ticker, row, names.get(ticker))
    return tickers | held, held, names


def load_daily_score_report(journal: object, *, report_id: str | None = None) -> dict[str, object] | None:
    """Only caller-scoped journal reads; archived links never recompute."""
    if report_id is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", report_id):
            raise ValueError("El identificador del informe no es válido.")
        reader = getattr(journal, "get_daily_score_report", None)
        if not callable(reader):
            raise ValueError("El historial de informes todavía no está disponible.")
        report = reader(report_id)
        if report is None:
            return None
        if not isinstance(report, Mapping) or seal_daily_score_report(report)["report_id"] != report_id:
            raise ValueError("No se pudo verificar esta versión del informe.")
        return dict(report)
    snapshots = journal.list_analysis_snapshots()
    tickers, held, names = _catalog(journal)
    return build_daily_score_report(snapshots, tickers=tickers, held_tickers=held, company_names=names)


def _display_note(value: object) -> str:
    return f"{value:g}" if isinstance(value, (float, int)) and not isinstance(value, bool) else "—"


def _display_day(value: object) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return "—" if pd.isna(parsed) else parsed.strftime("%d/%m")


def _table(rows: list[dict[str, object]]) -> pd.DataFrame:
    output = []
    for row in rows:
        values = {f"{'Última' if index == 0 else 'Anterior' if index == 1 else f'-{index} sesiones'}":
            f"{_display_day(item.get('session'))} · {_display_note(item.get('score'))}"
            for index, item in enumerate(row.get("history") or [])}
        reading = "Revisar datos" if row.get("quality", {}).get("block_entry") else row.get("label")
        output.append({"Empresa": f"{row['company_name']} ({row['ticker']})", **values,
            "Cambio diario": _display_note(row.get("change")), "Lectura": reading})
    return pd.DataFrame(output)


def render_daily_score_page(journal: object, *, report_id: str | None = None) -> None:
    st.subheader("Resumen diario · cinco sesiones")
    st.write("Primero tu cartera y después tus favoritas. Una nota de entrada baja no es una orden de vender lo que ya tienes.")
    try:
        report = load_daily_score_report(journal, report_id=report_id)
    except Exception:
        st.error("No se pudo leer este informe privado. No se han actualizado datos ni enviado correos.")
        return
    if report is None:
        st.warning("Este informe no existe o no está disponible para tu usuario. No se mostrará otra versión como si fuera la del correo.")
        return
    st.caption(f"Precios y señales guardados: {report.get('as_of') or 'sin sesión guardada'}. Consulta de solo lectura; no son cotizaciones en tiempo real.")
    if report_id:
        st.info("Estás viendo exactamente el informe enlazado en el correo, no un cálculo posterior.")
    rows = report.get("rows") or []
    if not rows:
        st.info("No hay empresas guardadas en tu cartera o favoritas para este resumen.")
        return
    if report.get("calendar_status") != "complete":
        st.warning("No se pudo verificar todo el calendario de sesiones; no se rellenan días ni notas ausentes.")
    unavailable = sum(row.get("score") is None for row in rows)
    pending = sum(row.get("quality", {}).get("block_entry") is True for row in rows)
    if unavailable or pending:
        st.warning(f"{unavailable} empresas sin nota en la última sesión y {pending} con datos que requieren revisión. Esto no genera señales de venta.")
    for label, selected in (("Mi cartera", [row for row in rows if row.get("held")]),
                            ("Favoritas · entradas fuertes primero", [row for row in rows if not row.get("held")])):
        st.markdown(f"#### {label}")
        if selected:
            st.dataframe(_table(selected), hide_index=True, width="stretch")
        else:
            st.caption("Sin empresas en este grupo.")
    st.caption("Cada celda muestra fecha de sesión y nota. — significa sin dato. Una variación de 1–2 puntos se describe como estable, sin modificar el score del motor.")
    alerts = report.get("alerts") or []
    if alerts:
        with st.expander("Avisos incluidos en este informe"):
            for alert in alerts:
                if isinstance(alert, Mapping):
                    st.write(f"{alert.get('company_name') or alert.get('ticker')}: {alert.get('title') or 'Revisar posición'}")
                    st.caption(str(alert.get("change_reason") or ""))
                    st.write(str(alert.get("next_step") or "Revisar la evidencia; no se ejecutan órdenes."))
    selected_ticker = st.selectbox("Qué cambió y qué revisar", [row["ticker"] for row in rows],
        format_func=lambda ticker: next(f"{row['company_name']} ({ticker})" for row in rows if row["ticker"] == ticker), key="daily_score_detail")
    row = next(row for row in rows if row["ticker"] == selected_ticker)
    st.write(row.get("change_reason"))
    st.write(row.get("chart_comment"))
    st.info(str(row.get("next_step") or "Revisar los datos antes de decidir."))
    reasons = row.get("quality", {}).get("reasons", [])
    if reasons:
        st.caption(str(reasons[0]))
    for incident in report.get("incidents") or []:
        if isinstance(incident, Mapping) and incident.get("ticker") == selected_ticker:
            st.write(str(incident.get("reason") or "Precio pendiente de verificar."))
            st.caption(f"Fecha del precio: {incident.get('as_of') or 'sin verificar'}")
    for position in report.get("positions") or []:
        if isinstance(position, Mapping) and position.get("ticker") == selected_ticker:
            st.write(str(position.get("reading") or "Revisar posición"))
            st.write(str(position.get("reason") or ""))
            st.caption(str(position.get("next_step") or ""))
    with st.expander("Datos y métodos · detalle opcional"):
        st.caption(f"Crecimiento {_display_note(row.get('growth_score'))} · Fuerza del precio {_display_note(row.get('momentum_score'))} · Contexto {_display_note(row.get('context_score'))}")
        for reason in reasons:
            st.write(str(reason))
        if row.get("revision_count"):
            st.caption("Hay revisiones dentro de una misma sesión; se muestra la última guardada y no se interpreta como un nuevo día.")
        st.caption("Calidad, valoración, tesis y riesgo conservan sus vistas y reglas independientes en Estrategias; no se promedian con esta nota.")
        st.caption(f"Fuente de la señal: {row.get('source') or 'no documentada'} · Calendario: {row.get('calendar')}")
        st.caption(f"Sesión del precio: {row.get('session') or 'sin dato'} · Análisis registrado: {row.get('recorded_at') or 'sin fecha de registro'}")
    st.caption(f"Informe de puntuaciones: {report['report_id']}")
