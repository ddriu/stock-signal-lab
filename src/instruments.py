"""Registro canónico de símbolos utilizados por brókeres y proveedores.

Una misma posición puede aparecer con un nombre corto en el bróker y necesitar
otro símbolo para descargar precios. Mantener esta resolución en un solo módulo
evita que cartera, favoritas, alertas e importaciones discrepen entre sí.
"""

from __future__ import annotations

from collections.abc import Mapping


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


def resolve_company_name(ticker: str, *sources: object) -> str:
    """Elige un nombre ya disponible sin confundir placeholders con empresas.

    Prioriza el nombre completo de los metadatos y permite probar otra fuente
    si sólo hay un símbolo, un valor vacío o un nulo serializado. No descarga
    datos ni modifica el identificador usado para abrir el análisis.
    """

    symbol = resolve_analysis_ticker(ticker)
    raw_symbol = str(ticker).strip()
    placeholders = {symbol, "", "NAN", "NAT", "NONE", "<NA>", "N/D"}
    # Un alias textual como «amazon» también puede aportar el nombre «Amazon».
    if raw_symbol == raw_symbol.upper():
        placeholders.add(raw_symbol)
    for source in sources:
        values = (
            (source.get(key) for key in ("longName", "name", "asset_name", "company_name", "Empresa", "shortName"))
            if isinstance(source, Mapping) else (source,)
        )
        for value in values:
            if not isinstance(value, str):
                continue
            name = value.strip()
            if name.upper() not in placeholders:
                return name
    return symbol
