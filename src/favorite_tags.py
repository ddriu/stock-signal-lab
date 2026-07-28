"""Etiquetas legibles y consistentes para organizar empresas favoritas."""

from __future__ import annotations

from collections.abc import Iterable
import re


FAVORITE_TAGS = (
    "Energía",
    "Biotecnología",
    "Tecnología",
    "Salud",
    "Consumo",
    "Finanzas",
    "Industria",
    "Defensa",
    "Espacio",
    "Cuántica",
    "Inmobiliario",
    "Materias primas",
    "ETF",
    "Fondo",
    "Apalancado",
    "Small cap",
    "Dividendos",
    "Otra",
)

TAG_CSS_CLASSES = {
    "Energía": "energy",
    "Biotecnología": "biotech",
    "Tecnología": "technology",
    "Salud": "health",
    "Consumo": "consumer",
    "Finanzas": "finance",
    "Industria": "industry",
    "Defensa": "defense",
    "Espacio": "space",
    "Cuántica": "quantum",
    "Inmobiliario": "real-estate",
    "Materias primas": "materials",
    "ETF": "etf",
    "Fondo": "fund",
    "Apalancado": "leveraged",
    "Small cap": "small-cap",
    "Dividendos": "dividend",
    "Otra": "other",
}

_SECTOR_TAGS = {
    "basic materials": "Materias primas",
    "communication services": "Tecnología",
    "consumer cyclical": "Consumo",
    "consumer defensive": "Consumo",
    "consumer discretionary": "Consumo",
    "consumer staples": "Consumo",
    "energy": "Energía",
    "financial services": "Finanzas",
    "financials": "Finanzas",
    "healthcare": "Salud",
    "industrials": "Industria",
    "real estate": "Inmobiliario",
    "technology": "Tecnología",
}


def favorite_tags_from_value(value: object) -> list[str]:
    """Convierte texto o secuencia en etiquetas admitidas, sin duplicados."""

    if value is None:
        return []
    if isinstance(value, str):
        candidates: Iterable[object] = re.split(r"[,;|]", value)
    elif isinstance(value, Iterable):
        candidates = value
    else:
        candidates = [value]

    allowed = {tag.casefold(): tag for tag in FAVORITE_TAGS}
    normalized: list[str] = []
    for candidate in candidates:
        tag = allowed.get(str(candidate).strip().casefold())
        if tag and tag not in normalized:
            normalized.append(tag)
    return normalized[:5]


def serialize_favorite_tags(value: object) -> str:
    """Formato estable y legible para SQLite y Supabase."""

    return ", ".join(favorite_tags_from_value(value))


def favorite_tag_css_class(tag: str) -> str:
    return TAG_CSS_CLASSES.get(tag, "other")


def suggest_favorite_tags(
    ticker: str,
    name: str = "",
    instrument_type: str = "",
    fundamentals: dict[str, object] | None = None,
) -> list[str]:
    """Propone etiquetas con tipo de activo, sector, industria y capitalización."""

    fundamentals = fundamentals or {}
    normalized_ticker = ticker.strip().upper()
    normalized_type = instrument_type.strip().casefold()
    sector = str(fundamentals.get("sector") or "").strip().casefold()
    industry = str(fundamentals.get("industry") or "").strip().casefold()
    searchable = f"{normalized_ticker} {name} {industry}".casefold()
    tags: list[str] = []

    if normalized_type == "etf" or " etf" in f" {searchable}":
        tags.append("ETF")
    elif normalized_type == "fondo" or any(
        token in searchable for token in (" fund", " fondo", " sicav")
    ):
        tags.append("Fondo")

    if any(
        token in searchable
        for token in ("biotech", "biotechnology", "biopharma", "therapeutics")
    ):
        tags.append("Biotecnología")
    else:
        sector_tag = _SECTOR_TAGS.get(sector)
        if sector_tag:
            tags.append(sector_tag)
        elif any(
            token in searchable
            for token in ("oil", "gas", "petroleum", "energy", "ypf")
        ):
            tags.append("Energía")
        elif any(
            token in searchable
            for token in (
                "semiconductor",
                "software",
                "technology",
                "digital",
                "cloud",
            )
        ):
            tags.append("Tecnología")

    if any(token in searchable for token in ("aerospace", "defense", "defence", "weapon")):
        tags.append("Defensa")
    if any(token in searchable for token in ("space", "satellite", "rocket")):
        tags.append("Espacio")
    if normalized_ticker in {"IONQ", "RGTI", "QBTS", "QUBT"} or "quantum" in searchable:
        tags.append("Cuántica")

    market_cap = fundamentals.get("marketCap")
    try:
        numeric_market_cap = float(market_cap) if market_cap is not None else None
    except (TypeError, ValueError):
        numeric_market_cap = None
    if (
        numeric_market_cap is not None
        and 0 < numeric_market_cap < 2_000_000_000
        and "ETF" not in tags
        and "Fondo" not in tags
    ):
        tags.append("Small cap")

    return favorite_tags_from_value(tags or ["Otra"])
