"""Normalización de estado al navegar entre favoritos y análisis."""

from __future__ import annotations

from collections.abc import Iterable
import re


GROWTH_RADAR_STRONG_LABELS = frozenset({"Entrada fuerte"})
GROWTH_RADAR_CANDIDATE_LABELS = frozenset({"Entrada candidata"})
GROWTH_RADAR_WATCH_LABELS = frozenset(
    {"Vigilancia activa", "Esperar mejor precio"}
)
GROWTH_RADAR_PENDING_LABELS = frozenset(
    {
        "Pendiente de fundamentales",
        "Datos empresariales parciales",
        "Datos empresariales insuficientes",
    }
)


def normalize_ticker(value: object) -> str:
    """Devuelve un símbolo uniforme sin aceptar valores vacíos."""

    return str(value or "").strip().upper()


_DIRECT_TICKER_PATTERN = re.compile(r"^[A-Z0-9^][A-Z0-9.\-^=]{0,19}$")


def direct_ticker_from_query(value: object) -> str | None:
    """Reconoce un ticker escrito directamente sin confundirlo con un nombre.

    Admite símbolos internacionales habituales como ``SAN.MC``, ``7974.T`` o
    ``BRK-B``. Una búsqueda con espacios se considera un nombre de empresa y
    debe pasar por el buscador de instrumentos.
    """

    ticker = normalize_ticker(value)
    if not ticker or not _DIRECT_TICKER_PATTERN.fullmatch(ticker):
        return None
    return ticker


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


def merge_analysis_ticker_sources(*sources: Iterable[object]) -> list[str]:
    """Une favoritas, revisadas y recientes manteniendo un orden estable."""

    merged: list[str] = []
    for source in sources:
        for value in source:
            ticker = normalize_ticker(value)
            if ticker and ticker not in merged:
                merged.append(ticker)
    return merged


def growth_radar_ticker_groups(
    readings: Iterable[tuple[object, object]],
) -> dict[str, list[str]]:
    """Agrupa los tickers del radar para ofrecer accesos rápidos navegables."""

    groups: dict[str, list[str]] = {
        "all": [],
        "strong": [],
        "candidates": [],
        "watch": [],
        "pending": [],
    }
    for raw_ticker, raw_label in readings:
        ticker = normalize_ticker(raw_ticker)
        label = str(raw_label or "").strip()
        if not ticker or ticker in groups["all"]:
            continue
        groups["all"].append(ticker)
        if label in GROWTH_RADAR_STRONG_LABELS:
            groups["strong"].append(ticker)
        if label in GROWTH_RADAR_CANDIDATE_LABELS:
            groups["candidates"].append(ticker)
        if label in GROWTH_RADAR_WATCH_LABELS:
            groups["watch"].append(ticker)
        if label in GROWTH_RADAR_PENDING_LABELS:
            groups["pending"].append(ticker)
    return groups
