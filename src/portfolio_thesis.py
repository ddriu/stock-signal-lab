"""Estado manual y persistente de la tesis de una posición.

La fotografía de cartera ya dispone de un campo de comentarios en ambos
backends. Se reutiliza con un marcador reservado para no añadir una migración
de base de datos sólo para este estado. El marcador nunca ejecuta una venta:
únicamente habilita la revisión roja de una posición en la portada.
"""

from __future__ import annotations

from typing import Iterable, Mapping

import pandas as pd

from src.data_loader import resolve_analysis_ticker
from src.portfolio_snapshot import latest_portfolio_snapshot


THESIS_INVALIDATED_MARKER = "[[SSL_TESIS_INVALIDADA]]"


def _ticker(value: object) -> str:
    raw = str(value or "").strip()
    return resolve_analysis_ticker(raw) if raw else ""


def _without_marker(comment: object) -> str:
    lines = [
        line.strip()
        for line in str(comment or "").splitlines()
        if line.strip() and not line.strip().startswith(THESIS_INVALIDATED_MARKER)
    ]
    return "\n".join(lines)


def thesis_invalidations(snapshot: pd.DataFrame) -> dict[str, str]:
    """Devuelve ``ticker -> motivo`` para la última fotografía disponible."""

    if snapshot.empty or "analysis_ticker" not in snapshot:
        return {}
    latest, _ = latest_portfolio_snapshot(snapshot)
    if latest.empty:
        return {}
    invalidated: dict[str, str] = {}
    comments = latest.get("comments", pd.Series("", index=latest.index))
    for raw_ticker, comment in zip(latest["analysis_ticker"], comments, strict=True):
        ticker = _ticker(raw_ticker)
        if not ticker:
            continue
        for line in str(comment or "").splitlines():
            stripped = line.strip()
            if not stripped.startswith(THESIS_INVALIDATED_MARKER):
                continue
            reason = stripped.removeprefix(THESIS_INVALIDATED_MARKER).strip(" :-")
            invalidated[ticker] = reason or "Ruptura de tesis declarada por el usuario"
            break
    return invalidated


def apply_thesis_invalidations(
    live_summary: Iterable[Mapping[str, object]],
    snapshot: pd.DataFrame,
) -> list[dict[str, object]]:
    """Inyecta el estado manual en el resumen que consume el motor de decisión."""

    invalidated = thesis_invalidations(snapshot)
    rows: list[dict[str, object]] = []
    for source in live_summary:
        row = dict(source)
        ticker = _ticker(row.get("Ticker"))
        if ticker in invalidated:
            row["Tesis invalidada"] = True
            row["Motivo tesis"] = invalidated[ticker]
        rows.append(row)
    return rows


def build_thesis_update_rows(
    snapshot: pd.DataFrame,
    ticker: str,
    *,
    invalidated: bool,
    reason: str = "",
) -> pd.DataFrame:
    """Prepara sólo las filas que debe actualizar el ``upsert`` existente."""

    normalized_ticker = _ticker(ticker)
    if not normalized_ticker:
        raise ValueError("Selecciona una posición válida.")
    normalized_reason = reason.strip()
    if invalidated and len(normalized_reason) < 10:
        raise ValueError(
            "Describe brevemente qué hecho ha invalidado la tesis (mínimo 10 caracteres)."
        )
    latest, _ = latest_portfolio_snapshot(snapshot)
    if latest.empty or "analysis_ticker" not in latest:
        raise ValueError("No hay una fotografía de cartera que se pueda actualizar.")
    resolved = latest["analysis_ticker"].fillna("").astype(str).map(_ticker)
    matching = latest.loc[resolved == normalized_ticker].copy()
    if matching.empty:
        raise ValueError("La posición elegida ya no aparece en la cartera actual.")
    if "comments" not in matching:
        matching["comments"] = ""
    matching["comments"] = matching["comments"].map(_without_marker)
    if invalidated:
        first_index = matching.index[0]
        existing = str(matching.at[first_index, "comments"] or "").strip()
        marker = f"{THESIS_INVALIDATED_MARKER} {normalized_reason}"
        matching.at[first_index, "comments"] = (
            f"{existing}\n{marker}" if existing else marker
        )
    return matching.reset_index(drop=True)
