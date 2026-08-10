"""Lecturas simples que unen señales de mercado y posiciones del usuario."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import pandas as pd


STRONG_ENTRY_LABELS = frozenset({"Entrada fuerte"})
CANDIDATE_ENTRY_LABELS = frozenset({"Entrada interesante", "Entrada candidata"})


def _ticker(value: object) -> str:
    return str(value or "").strip().upper()


def _number(value: object) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def build_portfolio_decision_rows(
    live_summary: Iterable[Mapping[str, object]],
    held_tickers: Iterable[object],
    *,
    allocations_pct: Mapping[str, float] | None = None,
    max_add_allocation_pct: float = 15.0,
) -> list[dict[str, object]]:
    """Clasifica las posiciones sin confundir mantener con comprar más."""

    by_ticker = {
        ticker: dict(row)
        for row in live_summary
        if (ticker := _ticker(row.get("Ticker")))
    }
    allocations = {
        _ticker(ticker): float(value)
        for ticker, value in (allocations_pct or {}).items()
        if _number(value) is not None
    }
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for value in held_tickers:
        ticker = _ticker(value)
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        source = by_ticker.get(ticker)
        allocation = allocations.get(ticker)
        if source is None:
            rows.append(
                {
                    "Ticker": ticker,
                    "Decisión": "Actualizar datos",
                    "Entrada": "Sin comprobar",
                    "Oportunidad": None,
                    "Peso": allocation,
                    "Motivo": "Falta un precio o análisis reciente.",
                }
            )
            continue

        position_label = str(source.get("Si ya la tienes") or "Revisar")
        entry_label = str(source.get("Lectura entrada") or "Sin comprobar")
        opportunity = _number(source.get("Oportunidad"))
        has_room = allocation is None or allocation <= max_add_allocation_pct
        attractive_entry = (
            entry_label in STRONG_ENTRY_LABELS.union(CANDIDATE_ENTRY_LABELS)
            and opportunity is not None
            and opportunity >= 65.0
        )

        if position_label == "Vender":
            decision = "Revisar venta"
            reason = "La tendencia principal o el límite de pérdida están dañados."
        elif position_label == "Reducir":
            decision = "Reducir"
            reason = "La señal de la posición se ha debilitado."
        elif position_label == "Mantener" and attractive_entry and has_room:
            decision = "Posible ampliar"
            reason = "Mantiene la tendencia y el precio vuelve a ofrecer una entrada razonable."
        elif position_label == "Mantener" and attractive_entry:
            decision = "Mantener"
            reason = "La entrada es atractiva, pero el peso actual aconseja no concentrar más."
        else:
            decision = "Mantener"
            reason = "No hay señal de salida, pero tampoco una entrada suficientemente clara."

        rows.append(
            {
                "Ticker": ticker,
                "Decisión": decision,
                "Entrada": entry_label,
                "Oportunidad": opportunity,
                "Peso": allocation,
                "Motivo": reason,
            }
        )
    return rows


def entry_opportunity_rows(
    live_summary: Iterable[Mapping[str, object]],
    held_tickers: Iterable[object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Separa entradas fuertes y candidatas que aún no están en cartera."""

    held = {_ticker(value) for value in held_tickers}
    strong: list[dict[str, object]] = []
    candidates: list[dict[str, object]] = []
    for raw_row in live_summary:
        row = dict(raw_row)
        ticker = _ticker(row.get("Ticker"))
        if not ticker or ticker in held:
            continue
        entry_label = str(row.get("Lectura entrada") or "")
        if entry_label in STRONG_ENTRY_LABELS:
            strong.append(row)
        elif entry_label in CANDIDATE_ENTRY_LABELS:
            candidates.append(row)
    sort_key = lambda row: (
        _number(row.get("Momento entrada")) or -1,
        _number(row.get("Oportunidad")) or -1,
    )
    strong.sort(key=sort_key, reverse=True)
    candidates.sort(key=sort_key, reverse=True)
    return strong, candidates
