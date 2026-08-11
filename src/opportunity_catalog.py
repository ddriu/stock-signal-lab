"""Catálogo estable de oportunidades a partir de favoritas y análisis guardados."""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from src.data_loader import resolve_analysis_ticker


def _ticker(value: object) -> str:
    ticker = str(value or "").strip().upper()
    return resolve_analysis_ticker(ticker) if ticker else ""


def _optional_number(value: object) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_text(value: object) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return "Sin comprobar"
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return str(value)
    return parsed.date().isoformat()


def _latest_snapshots(snapshots: pd.DataFrame) -> dict[str, dict[str, object]]:
    if snapshots.empty or "ticker" not in snapshots.columns:
        return {}
    ordered = snapshots.copy()
    ordered["_ticker"] = ordered["ticker"].map(_ticker)
    if "analyzed_at" in ordered.columns:
        ordered["_analyzed_at"] = pd.to_datetime(
            ordered["analyzed_at"], errors="coerce"
        )
        ordered = ordered.sort_values("_analyzed_at", ascending=False, na_position="last")
    latest: dict[str, dict[str, object]] = {}
    for row in ordered.to_dict("records"):
        ticker = _ticker(row.get("_ticker"))
        if ticker and ticker not in latest:
            latest[ticker] = row
    return latest


def _saved_row(ticker: str, snapshot: dict[str, object]) -> dict[str, object]:
    return {
        "Ticker": ticker,
        "Oportunidad": _optional_number(snapshot.get("opportunity_score")),
        "Confianza datos": None,
        "Lectura conjunta": str(
            snapshot.get("opportunity_label") or "Análisis guardado"
        ),
        "Calidad empresa": _optional_number(snapshot.get("company_score")),
        "Valoración": _optional_number(snapshot.get("valuation_score")),
        "Momento entrada": _optional_number(snapshot.get("entry_score")),
        "Fuerza relativa": _optional_number(snapshot.get("relative_score")),
        "Riesgo controlado": _optional_number(snapshot.get("risk_score")),
        "Lectura entrada": str(snapshot.get("entry_label") or "Sin lectura"),
        "Si ya la tienes": str(snapshot.get("position_label") or "Revisar"),
        "Cierre": _optional_number(snapshot.get("price")),
        "RSI": None,
        "Fuerza 3 meses": None,
        "Desde su máximo": None,
        "Actividad": None,
        "Nuevo máximo reciente": "N/D",
        "Fecha": _date_text(snapshot.get("analyzed_at")),
        "Comprobación": "Pendiente de actualizar",
        "Origen": "Último análisis guardado",
    }


def _pending_row(ticker: str) -> dict[str, object]:
    return {
        "Ticker": ticker,
        "Oportunidad": None,
        "Confianza datos": None,
        "Lectura conjunta": "Pendiente de comprobar",
        "Calidad empresa": None,
        "Valoración": None,
        "Momento entrada": None,
        "Fuerza relativa": None,
        "Riesgo controlado": None,
        "Lectura entrada": "Sin análisis previo",
        "Si ya la tienes": "Revisar",
        "Cierre": None,
        "RSI": None,
        "Fuerza 3 meses": None,
        "Desde su máximo": None,
        "Actividad": None,
        "Nuevo máximo reciente": "N/D",
        "Fecha": "Sin comprobar",
        "Comprobación": "Sin análisis previo",
        "Origen": "Favorita",
    }


def build_opportunity_catalog(
    favorite_tickers: Iterable[object],
    snapshots: pd.DataFrame,
    live_summary: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    """Incluye toda favorita y prioriza los datos recalculados en esta sesión.

    El catálogo no presenta una fotografía antigua como si fuera actual: conserva
    sus notas para orientar al usuario y la marca expresamente como pendiente.
    """

    favorites: list[str] = []
    for value in favorite_tickers:
        ticker = _ticker(value)
        if ticker and ticker not in favorites:
            favorites.append(ticker)

    live_by_ticker: dict[str, dict[str, object]] = {}
    live_order: list[str] = []
    for raw_row in live_summary:
        row = dict(raw_row)
        ticker = _ticker(row.get("Ticker"))
        if not ticker:
            continue
        row["Ticker"] = ticker
        row["Comprobación"] = "Actualizado en esta sesión"
        row["Origen"] = "Datos actuales"
        live_by_ticker[ticker] = row
        if ticker not in live_order:
            live_order.append(ticker)

    latest = _latest_snapshots(snapshots)
    ordered_tickers = [*favorites]
    ordered_tickers.extend(
        ticker for ticker in live_order if ticker not in ordered_tickers
    )

    catalog: list[dict[str, object]] = []
    for ticker in ordered_tickers:
        if ticker in live_by_ticker:
            catalog.append(live_by_ticker[ticker])
        elif ticker in latest:
            catalog.append(_saved_row(ticker, latest[ticker]))
        else:
            catalog.append(_pending_row(ticker))
    return catalog
