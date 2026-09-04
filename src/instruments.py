"""Registro canónico de símbolos utilizados por brókeres y proveedores.

Una misma posición puede aparecer con un nombre corto en el bróker y necesitar
otro símbolo para descargar precios. Mantener esta resolución en un solo módulo
evita que cartera, favoritas, alertas e importaciones discrepen entre sí.
"""

from __future__ import annotations


ANALYSIS_TICKER_ALIASES: dict[str, str] = {
    "6VO": "RDDT",
    "AMAZON": "AMZN",
    "AMAZON.COM": "AMZN",
    "AMZ": "AMZN",
    "CEBS": "CEBS.DE",
    "NETFLIX": "NFLX",
    "ORACLE": "ORCL",
    "REDDIT": "RDDT",
    "SERVICE NOW": "NOW",
    "SERVICENOW": "NOW",
    "7974 / NTDOY": "NTDOY",
    "KAP": "KAP.IL",
    "1801": "1801.HK",
    "05Y": "05Y.F",
}


def normalize_instrument_key(value: object) -> str:
    """Normaliza un alias sin alterar sufijos de mercado significativos."""

    return " ".join(str(value or "").strip().upper().split())


def resolve_analysis_ticker(value: object) -> str:
    """Devuelve el símbolo único que deben consultar los proveedores."""

    key = normalize_instrument_key(value)
    if not key:
        raise ValueError("El ticker no puede estar vacío.")
    return ANALYSIS_TICKER_ALIASES.get(key, key)

