"""Normalización de estado al navegar entre favoritos y análisis."""

from __future__ import annotations

from collections.abc import Iterable


def normalize_ticker(value: object) -> str:
    """Devuelve un símbolo uniforme sin aceptar valores vacíos."""

    return str(value or "").strip().upper()


def sanitize_favorite_selection(
    selected: object,
    allowed_tickers: Iterable[str],
) -> list[str]:
    """Evita que un ticker temporal invalide el multiselector de favoritos."""

    allowed = {normalize_ticker(ticker) for ticker in allowed_tickers}
    values = selected if isinstance(selected, (list, tuple, set)) else []
    clean: list[str] = []
    for value in values:
        ticker = normalize_ticker(value)
        if ticker and ticker in allowed and ticker not in clean:
            clean.append(ticker)
    return clean


def analysis_refresh_tickers(
    selected_tickers: Iterable[str],
    held_tickers: Iterable[str],
    *,
    pending_ticker: object = "",
    active_ticker: object = "",
) -> list[str]:
    """Mantiene abierto el ticker directo al actualizar, aunque no sea favorito."""

    ordered = [pending_ticker, active_ticker, *selected_tickers, *held_tickers]
    result: list[str] = []
    for value in ordered:
        ticker = normalize_ticker(value)
        if ticker and ticker not in result:
            result.append(ticker)
    return result
