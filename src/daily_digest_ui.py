"""Simple private score history; opening this view never refreshes or sends."""
from __future__ import annotations

import re
import math
from collections.abc import Mapping

import pandas as pd
import streamlit as st

from src.daily_digest import (
    build_daily_score_report, daily_score_coverage, daily_score_reading,
    daily_score_sort_key, seal_daily_score_report,
)
from src.analysis_data_quality import METRIC_NAMES
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
    if isinstance(value, bool):
        return "—"
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return "—"
    return f"{number:g}" if math.isfinite(number) else "—"


def _display_day(value: object) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return "—" if pd.isna(parsed) else parsed.strftime("%d/%m")


def _table(rows: list[dict[str, object]]) -> pd.DataFrame:
    """One current reading per company; the dated history belongs in detail."""
    output = []
    for row in rows:
        reading = daily_score_reading(row)
        output.append({"Empresa": f"{row['company_name']} ({row['ticker']})",
            "Nota · sesión": f"{_display_note(row.get('score'))} · {_display_day(row.get('session'))}",
            "Cambio": _display_note(row.get("change")), "Señal del motor": reading["signal"],
            "Verificación": reading["verification"], "Revisión de compra": reading["decision"]})
    return pd.DataFrame(output)


def _history(row: dict[str, object]) -> pd.DataFrame:
    return pd.DataFrame([
        {"Sesión": _display_day(item.get("session")), "Nota": _display_note(item.get("score"))}
        for item in row.get("history") or [] if isinstance(item, Mapping)
    ])


def render_daily_score_page(journal: object, *, report_id: str | None = None) -> None:
    st.subheader("Resumen diario")
    st.write("Primero tu cartera y después las posibles entradas. La señal del motor y la verificación de sus datos son dos cosas distintas; ninguna ejecuta una compra o una venta.")
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
    coverage = daily_score_coverage(rows)
    unavailable, pending = coverage["missing_score"], coverage["pending_data"]
    if unavailable or pending:
        st.warning(f"{pending} de {len(rows)} empresas tienen la compra sin confirmar por datos pendientes; {unavailable} de ellas no tienen nota de la última sesión. No son señales de venta.")
    held = sorted([row for row in rows if row.get("held")], key=daily_score_sort_key)
    favorites = sorted([row for row in rows if not row.get("held")], key=daily_score_sort_key)
    st.markdown("#### Mi cartera")
    if held:
        st.dataframe(_table(held), hide_index=True, width="stretch")
    else:
        st.caption("Sin posiciones guardadas en este informe.")
    st.markdown("#### Favoritas · señales de entrada")
    favorite_coverage = daily_score_coverage(favorites)
    verified, strong_pending = favorite_coverage["strong_verified"], favorite_coverage["strong_pending"]
    st.caption(f"{verified + strong_pending} entradas fuertes del motor: {verified} con datos suficientes y {strong_pending} pendientes de verificar. Una candidatura no es una orden de compra.")
    featured = [row for row in favorites if daily_score_reading(row)["signal"] == "Entrada fuerte" and row.get("score") is not None
        or not daily_score_reading(row)["blocked"] and row.get("status") == "entry_candidate"]
    others = [row for row in favorites if row not in featured]
    if featured:
        st.dataframe(_table(featured), hide_index=True, width="stretch")
    else:
        st.info("No hay entradas fuertes ni candidatas verificadas en este informe. Los datos pendientes no demuestran que no existan oportunidades.")
    if others:
        with st.expander(f"Otras favoritas · {len(others)}"):
            st.dataframe(_table(others), hide_index=True, width="stretch")
    st.caption("Nota sobre 100; no es una probabilidad de ganar. — significa sin dato. El histórico de cinco sesiones está en el detalle de cada empresa; no se rellenan notas ausentes.")
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
    reading = daily_score_reading(row)
    st.write(f"Señal del motor: {reading['signal']} · Nota {_display_note(row.get('score'))}/100")
    st.write(f"Verificación: {reading['verification']}")
    st.write(row.get("change_reason"))
    st.write(row.get("chart_comment"))
    st.info(f"{reading['decision']}. {row.get('next_step') or 'Revisar los datos antes de decidir.'}")
    reasons = reading["reasons"]
    if reasons:
        st.write("Qué falta o qué conviene comprobar:")
        for reason in reasons:
            st.caption(str(reason))
    st.caption(f"Fuente: {row.get('source') or 'no documentada'} · Sesión del precio: {row.get('session') or 'sin dato'} · Análisis registrado: {row.get('recorded_at') or 'sin fecha de registro'}")
    with st.expander("Historial · cinco sesiones"):
        st.dataframe(_history(row), hide_index=True, width="stretch")
        st.caption("Una variación de 1–2 puntos se describe como estable sin modificar la nota del motor. Un cambio de fuente o método puede impedir la comparación diaria.")
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
        if row.get("revision_count"):
            st.caption("Hay revisiones dentro de una misma sesión; se muestra la última guardada y no se interpreta como un nuevo día.")
        st.caption("Calidad, valoración, tesis y riesgo conservan sus vistas y reglas independientes en Estrategias; no se promedian con esta nota.")
        st.caption(f"Versión del análisis: {row.get('policy_version') or 'no documentada'} · Calendario: {row.get('calendar') or 'sin verificar'}")
        quality = row.get("quality") or {}
        st.caption(f"Método de recuperación de datos: {quality.get('input_version') or 'no documentado en este informe'}")
        periods = quality.get("metric_periods") or {}
        if isinstance(periods, Mapping) and periods:
            st.dataframe(pd.DataFrame([
                {"Métrica": METRIC_NAMES.get(key, key), "Fuente": value.get("source"),
                 "Periodo": value.get("periodicity"), "Cierre contable": value.get("period_end"),
                 "Publicación": value.get("filed_at") or "no documentada"}
                for key, value in periods.items() if isinstance(value, Mapping)
            ]), hide_index=True, width="stretch")
    st.caption(f"Informe de puntuaciones: {report['report_id']}")
