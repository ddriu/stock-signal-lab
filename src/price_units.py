"""Normalización de unidades de cotización y formato monetario.

Algunas bolsas publican las series históricas en subunidades: por ejemplo,
Yahoo identifica muchas acciones de Londres como ``GBp`` (peniques), aunque la
moneda que entiende una persona sea la libra. Mantener esta conversión en un
único punto evita que precio, ATR, soportes, stops y volumen monetario usen
escalas diferentes.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


PRICE_COLUMNS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class QuoteUnit:
    source_currency: str
    display_currency: str
    price_scale: float
    symbol: str

    @property
    def converted_from_subunit(self) -> bool:
        return self.price_scale != 1.0


_SUBUNIT_QUOTES: dict[str, tuple[str, float, str]] = {
    "GBp": ("GBP", 0.01, "£"),
    "GBX": ("GBP", 0.01, "£"),
    "ZAc": ("ZAR", 0.01, "R"),
    "ZAC": ("ZAR", 0.01, "R"),
    "ILA": ("ILS", 0.01, "₪"),
}

_CURRENCY_SYMBOLS = {
    "EUR": "€",
    "GBP": "£",
    "USD": "$",
    "JPY": "¥",
    "CNY": "¥",
    "HKD": "HK$",
    "KRW": "₩",
    "CHF": "CHF",
    "CAD": "C$",
    "AUD": "A$",
    "ZAR": "R",
    "ILS": "₪",
}


def resolve_quote_unit(currency: object) -> QuoteUnit:
    """Describe cómo interpretar una moneda sin perder el caso de ``GBp``."""

    raw = str(currency or "").strip()
    if raw in _SUBUNIT_QUOTES:
        normalized, scale, symbol = _SUBUNIT_QUOTES[raw]
        return QuoteUnit(raw, normalized, scale, symbol)
    normalized = raw.upper()
    return QuoteUnit(
        source_currency=raw,
        display_currency=normalized,
        price_scale=1.0,
        symbol=_CURRENCY_SYMBOLS.get(normalized, normalized),
    )


def normalize_price_frame_units(
    frame: pd.DataFrame,
    currency: object,
) -> pd.DataFrame:
    """Devuelve una copia con OHLC expresado en la unidad monetaria principal.

    Volumen representa títulos negociados y no se escala. Los atributos dejan
    constancia de la transformación para que una capa posterior pueda mostrarla
    y para evitar una doble conversión accidental.
    """

    data = frame.copy()
    data.attrs = dict(frame.attrs)
    unit = resolve_quote_unit(currency)
    if data.attrs.get("quote_units_normalized"):
        return data
    if unit.converted_from_subunit:
        for column in PRICE_COLUMNS:
            if column in data:
                data[column] = pd.to_numeric(data[column], errors="coerce") * unit.price_scale
    data.attrs.update(
        {
            "source_currency": unit.source_currency,
            "display_currency": unit.display_currency,
            "price_scale": unit.price_scale,
            "quote_units_normalized": True,
        }
    )
    return data


def format_quote_price(value: float, currency: object, decimals: int = 2) -> str:
    """Formatea un precio que ya está normalizado en la moneda principal."""

    unit = resolve_quote_unit(currency)
    number = f"{float(value):,.{decimals}f}"
    return f"{unit.symbol}{number}" if unit.symbol else number
