"""Presentation-only coverage: an unavailable reading is not a measured zero."""
from __future__ import annotations

from math import isfinite

import pandas as pd

from src.market_data_quality import quote_freshness
from src.opportunity_catalog import VERIFIED_RADAR_READING


def measured_count_label(count: int, *, available: int, total: int) -> str:
    """A partial positive is a lower bound; a partial zero is still unknown."""
    if available <= 0 or total <= 0 or (available < total and count == 0):
        return "N/D"
    return f"≥ {count}" if available < total else str(count)


def opportunity_coverage(rows, *, reference_date=None):
    """Count only dated, recent calculations, without changing engine results.

    Historical rows remain available in the caller's table, but neither a
    failed analysis row nor merely recalculating old prices completes a review.
    This uses the existing shared freshness rule, not a new strategy threshold.
    """
    reference_date = reference_date or pd.Timestamp.now(tz="Europe/Madrid").date()
    rows = list(rows)
    available = []
    for row in rows:
        if row.get("Comprobación") not in {"Actualizado en esta sesión", VERIFIED_RADAR_READING}:
            continue
        score = row.get("Momento entrada")
        try:
            valid_score = not isinstance(score, bool) and isfinite(float(score)) and 0 <= float(score) <= 100
        except (TypeError, ValueError, OverflowError):
            valid_score = False
        label = str(row.get("Lectura entrada") or "").strip()
        if not valid_score or not label or any(word in label.casefold() for word in (
            "sin datos", "sin lectura", "sin análisis", "pendiente", "insuficiente",
        )):
            continue
        if quote_freshness(row.get("Fecha"), reference_date=reference_date).fresh:
            available.append(row)
    dates = sorted({pd.Timestamp(row["Fecha"]).date() for row in available})
    strong = sum(row["Lectura entrada"] == "Entrada fuerte" for row in available)
    candidate = sum(row["Lectura entrada"] in {"Entrada interesante", "Entrada candidata"} for row in available)
    total, reviewed = len(rows), len(available)
    return {
        "total": total, "available": reviewed, "pending": total - reviewed,
        "strong": strong, "candidate": candidate, "waiting": reviewed - strong - candidate,
        "status": "unavailable" if not reviewed else "partial" if reviewed < total else "complete",
        "tickers": {str(row.get("Ticker") or "") for row in available},
        "oldest_date": dates[0] if dates else None,
        "newest_date": dates[-1] if dates else None,
    }
