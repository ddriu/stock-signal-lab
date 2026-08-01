"""Descarga y normalización de precios históricos."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from io import StringIO

import pandas as pd
import requests
import yfinance as yf

from src.data_sources import (
    ExternalDataError,
    download_sec_fundamental_snapshot,
    merge_fundamental_sources,
)


REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")
STOOQ_DAILY_URL = "https://stooq.com/q/d/l/"
STOOQ_SUFFIXES = {
    "AS": "NL",
    "L": "UK",
    "MC": "ES",
    "MI": "IT",
    "PA": "FR",
    "SW": "CH",
    "TO": "CA",
}
STOOQ_INDEX_SYMBOLS = {
    "^DJI": "^DJI",
    "^GSPC": "^SPX",
    "^IXIC": "^NDQ",
}


class DataDownloadError(RuntimeError):
    """Error recuperable al obtener o validar precios."""


@dataclass(frozen=True)
class TickerSearchResult:
    """Resultado legible del buscador de instrumentos de Yahoo."""

    ticker: str
    name: str
    exchange: str
    instrument_type: str
    country: str = ""
    currency: str = ""
    listing_type: str = ""

    @property
    def label(self) -> str:
        market = f" · {self.exchange}" if self.exchange else ""
        return f"{self.name} ({self.ticker}){market}"

    @property
    def details(self) -> str:
        """Descripción corta para distinguir cotizaciones del mismo activo."""

        values = [
            self.instrument_type,
            self.country,
            self.currency,
            self.listing_type,
        ]
        return " · ".join(value for value in values if value)

    @property
    def market_group(self) -> str:
        """Grupo sencillo utilizado por el filtro del buscador."""

        return self.country or self.exchange or "Otros mercados"


_SUFFIX_MARKETS: dict[str, tuple[str, str, str]] = {
    "AS": ("Países Bajos", "EUR", "Acción local"),
    "AX": ("Australia", "AUD", "Acción local"),
    "BA": ("Argentina", "ARS", "Acción local"),
    "BR": ("Bélgica", "EUR", "Acción local"),
    "CO": ("Dinamarca", "DKK", "Acción local"),
    "DE": ("Alemania", "EUR", "Acción local"),
    "F": ("Alemania", "EUR", "Cotización en Fráncfort"),
    "HE": ("Finlandia", "EUR", "Acción local"),
    "HK": ("Hong Kong", "HKD", "Acción local"),
    "IL": ("Londres internacional", "USD", "GDR internacional"),
    "IS": ("Turquía", "TRY", "Acción local"),
    "JO": ("Sudáfrica", "ZAR", "Acción local"),
    "KQ": ("Corea del Sur", "KRW", "Acción local"),
    "KS": ("Corea del Sur", "KRW", "Acción local"),
    "L": ("Reino Unido", "GBP", "Acción local"),
    "LS": ("Portugal", "EUR", "Acción local"),
    "MC": ("España", "EUR", "Acción local"),
    "MI": ("Italia", "EUR", "Acción local"),
    "MX": ("México", "MXN", "Acción local"),
    "NZ": ("Nueva Zelanda", "NZD", "Acción local"),
    "OL": ("Noruega", "NOK", "Acción local"),
    "PA": ("Francia", "EUR", "Acción local"),
    "SA": ("Brasil", "BRL", "Acción local"),
    "SI": ("Singapur", "SGD", "Acción local"),
    "ST": ("Suecia", "SEK", "Acción local"),
    "SW": ("Suiza", "CHF", "Acción local"),
    "T": ("Japón", "JPY", "Acción local"),
    "TA": ("Israel", "ILS", "Acción local"),
    "TO": ("Canadá", "CAD", "Acción local"),
    "V": ("Canadá", "CAD", "Acción local"),
    "VI": ("Austria", "EUR", "Acción local"),
    "WA": ("Polonia", "PLN", "Acción local"),
}


def _market_metadata(
    symbol: str,
    exchange: str,
    currency: str = "",
) -> tuple[str, str, str]:
    """Infiere país, moneda y clase de cotización con información pública de bolsa."""

    suffix = symbol.rsplit(".", 1)[1] if "." in symbol else ""
    if suffix in _SUFFIX_MARKETS:
        country, inferred_currency, listing_type = _SUFFIX_MARKETS[suffix]
        return country, currency or inferred_currency, listing_type

    normalized_exchange = exchange.upper()
    exchange_rules = (
        (("NASDAQ", "NMS", "NGM", "NCM", "NYSE", "NYQ", "AMEX", "ASE", "PCX", "BATS"),
         ("Estados Unidos", "USD", "Cotización estadounidense")),
        (("OTC", "PNK"), ("Estados Unidos", "USD", "Cotización OTC")),
        (("TOKYO", "JPX"), ("Japón", "JPY", "Acción local")),
        (("LONDON", "LSE"), ("Reino Unido", "GBP", "Acción local")),
        (("MADRID", "BME"), ("España", "EUR", "Acción local")),
        (("TORONTO", "TSX"), ("Canadá", "CAD", "Acción local")),
        (("XETRA", "FRANKFURT"), ("Alemania", "EUR", "Acción local")),
    )
    for aliases, metadata in exchange_rules:
        if any(alias in normalized_exchange for alias in aliases):
            country, inferred_currency, listing_type = metadata
            return country, currency or inferred_currency, listing_type
    return "Otros mercados", currency, ""


_CURATED_INTERNATIONAL_LISTINGS: tuple[tuple[tuple[str, ...], TickerSearchResult], ...] = (
    (
        ("kazatomprom", "national atomic company", "kap.il"),
        TickerSearchResult(
            ticker="KAP.IL",
            name="NAC Kazatomprom",
            exchange="London IOB",
            instrument_type="Acción",
            country="Londres internacional",
            currency="USD",
            listing_type="GDR internacional",
        ),
    ),
    (
        ("nintendo", "7974", "7974.t"),
        TickerSearchResult(
            ticker="7974.T",
            name="Nintendo Co., Ltd.",
            exchange="Tokyo",
            instrument_type="Acción",
            country="Japón",
            currency="JPY",
            listing_type="Acción local",
        ),
    ),
    (
        ("nintendo", "ntdoy"),
        TickerSearchResult(
            ticker="NTDOY",
            name="Nintendo Co., Ltd.",
            exchange="OTC",
            instrument_type="Acción",
            country="Estados Unidos",
            currency="USD",
            listing_type="ADR / OTC",
        ),
    ),
)


def _curated_search_results(query: str) -> list[TickerSearchResult]:
    normalized_query = query.strip().casefold()
    if not normalized_query:
        return []
    return [
        result
        for aliases, result in _CURATED_INTERNATIONAL_LISTINGS
        if any(normalized_query in alias or alias in normalized_query for alias in aliases)
    ]


def normalize_ticker(ticker: str) -> str:
    value = ticker.strip().upper()
    if not value:
        raise ValueError("El ticker no puede estar vacío.")
    return value


def search_instruments(query: str, max_results: int = 12) -> list[TickerSearchResult]:
    """Busca acciones y ETF por nombre o símbolo, sin exigir conocer el ticker."""

    cleaned = query.strip()
    if len(cleaned) < 2:
        return []
    requested = max(1, min(int(max_results), 25))
    curated_results = _curated_search_results(cleaned)
    try:
        try:
            search = yf.Search(
                cleaned,
                max_results=requested,
                news_count=0,
                lists_count=0,
                include_cb=False,
                include_nav_links=False,
                include_research=False,
                include_cultural_assets=False,
                enable_fuzzy_query=True,
                raise_errors=True,
            )
        except TypeError:
            # Compatibilidad con versiones anteriores admitidas por requirements.
            search = yf.Search(
                cleaned,
                max_results=requested,
                news_count=0,
            )
        quotes = search.quotes
    except Exception as exc:
        if curated_results:
            return curated_results[:requested]
        raise DataDownloadError(
            "El buscador de empresas no respondió. Puedes escribir el ticker manualmente."
        ) from exc

    results: list[TickerSearchResult] = []
    seen: set[str] = set()
    for result in curated_results:
        results.append(result)
        seen.add(result.ticker)
    for quote in quotes if isinstance(quotes, list) else []:
        if not isinstance(quote, dict):
            continue
        quote_type = str(quote.get("quoteType") or "").upper()
        if quote_type and quote_type not in {"EQUITY", "ETF"}:
            continue
        symbol = str(quote.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        name = str(
            quote.get("longname")
            or quote.get("shortname")
            or quote.get("longName")
            or quote.get("shortName")
            or symbol
        ).strip()
        exchange = str(
            quote.get("exchDisp")
            or quote.get("exchange")
            or quote.get("exchangeDisplay")
            or ""
        ).strip()
        quoted_currency = str(quote.get("currency") or "").strip().upper()
        country, currency, listing_type = _market_metadata(
            symbol,
            exchange,
            quoted_currency,
        )
        instrument_type = "ETF" if quote_type == "ETF" else "Acción"
        results.append(
            TickerSearchResult(
                ticker=symbol,
                name=name,
                exchange=exchange,
                instrument_type=instrument_type,
                country=country,
                currency=currency,
                listing_type=listing_type,
            )
        )
        seen.add(symbol)
    return results[:requested]


def _stooq_symbol(symbol: str) -> str:
    """Convierte tickers habituales de Yahoo al formato utilizado por Stooq."""

    if symbol in STOOQ_INDEX_SYMBOLS:
        return STOOQ_INDEX_SYMBOLS[symbol]
    if symbol.startswith("^"):
        return symbol
    if "." not in symbol:
        return f"{symbol}.US"
    base, suffix = symbol.rsplit(".", 1)
    return f"{base}.{STOOQ_SUFFIXES.get(suffix, suffix)}"


def _download_stooq_prices(
    symbol: str,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
) -> pd.DataFrame:
    """Obtiene CSV diario de Stooq como respaldo cuando Yahoo no responde."""

    try:
        response = requests.get(
            STOOQ_DAILY_URL,
            params={
                "s": _stooq_symbol(symbol).lower(),
                "d1": start_ts.strftime("%Y%m%d"),
                "d2": end_ts.strftime("%Y%m%d"),
                "i": "d",
            },
            headers={"User-Agent": "stock-signal-lab/1.0"},
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise DataDownloadError(f"Stooq no respondió para {symbol}: {exc}") from exc

    text = response.text.strip()
    if not text or text.lower().startswith("no data"):
        raise DataDownloadError(f"Stooq no encontró precios para {symbol}.")
    try:
        frame = pd.read_csv(StringIO(text))
    except (pd.errors.ParserError, UnicodeDecodeError) as exc:
        raise DataDownloadError(f"Stooq devolvió datos inválidos para {symbol}.") from exc
    if frame.empty or "Date" not in frame.columns:
        raise DataDownloadError(f"Stooq no encontró precios para {symbol}.")

    frame = frame.set_index("Date")
    frame.attrs["provider"] = "Stooq"
    return frame


def download_prices(
    ticker: str,
    start: date | str,
    end: date | str,
    *,
    auto_adjust: bool = True,
) -> pd.DataFrame:
    """Descarga OHLCV diario y devuelve un índice temporal ordenado.

    yfinance interpreta ``end`` como exclusivo; se suma un día para que la fecha
    elegida por el usuario quede incluida.
    """

    symbol = normalize_ticker(ticker)
    start_ts = pd.Timestamp(start).normalize()
    end_ts = pd.Timestamp(end).normalize()
    if start_ts >= end_ts:
        raise ValueError("La fecha inicial debe ser anterior a la final.")

    yahoo_error = ""
    try:
        frame = yf.download(
            symbol,
            start=start_ts.date().isoformat(),
            end=(end_ts + timedelta(days=1)).date().isoformat(),
            auto_adjust=auto_adjust,
            progress=False,
            actions=False,
            threads=False,
        )
    except Exception as exc:  # yfinance puede lanzar errores de red heterogéneos
        frame = pd.DataFrame()
        yahoo_error = str(exc)

    if frame.empty:
        try:
            frame = _download_stooq_prices(symbol, start_ts, end_ts)
        except DataDownloadError as exc:
            detail = (
                f" Yahoo indicó: {yahoo_error}."
                if yahoo_error
                else " Yahoo no devolvió precios."
            )
            raise DataDownloadError(
                f"No se encontraron precios para {symbol} en ese intervalo."
                f"{detail} {exc}"
            ) from exc

    if isinstance(frame.columns, pd.MultiIndex):
        # yfinance reciente devuelve (campo, ticker) incluso para un símbolo.
        frame.columns = frame.columns.get_level_values(0)

    frame = frame.rename(columns=lambda column: str(column).strip().lower())
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise DataDownloadError(f"Faltan columnas para {symbol}: {', '.join(missing)}")

    result = frame.loc[:, list(REQUIRED_COLUMNS)].copy()
    result.index = pd.to_datetime(result.index).tz_localize(None)
    result = result[~result.index.duplicated(keep="last")].sort_index()
    result = result.apply(pd.to_numeric, errors="coerce")
    result = result.dropna(subset=["open", "high", "low", "close"])
    result["volume"] = result["volume"].fillna(0.0)
    result.attrs["ticker"] = symbol
    result.attrs["provider"] = frame.attrs.get("provider", "Yahoo Finance")
    return result


def download_fundamental_snapshot(ticker: str) -> dict[str, object]:
    """Combina una fotografía de Yahoo con cuentas oficiales SEC disponibles.

    La respuesta puede ser parcial. Yahoo aporta contexto y valoración; para
    tickers registrados en Estados Unidos, las métricas contables calculadas
    desde EDGAR prevalecen. Si SEC no responde, Yahoo mantiene operativa la app.
    """

    symbol = normalize_ticker(ticker)
    fields = {
        "symbol",
        "shortName",
        "longName",
        "country",
        "sector",
        "industry",
        "currency",
        "returnOnEquity",
        "profitMargins",
        "operatingMargins",
        "revenueGrowth",
        "earningsGrowth",
        "debtToEquity",
        "currentRatio",
        "freeCashflow",
        "marketCap",
        "forwardPE",
        "trailingPE",
        "priceToBook",
        "enterpriseToEbitda",
        "pegRatio",
        "sharesOutstanding",
    }
    warnings: list[str] = []
    try:
        info = yf.Ticker(symbol).get_info()
    except Exception as exc:
        info = {}
        warnings.append(f"Yahoo no pudo aportar fundamentales de {symbol}: {exc}")
    if not isinstance(info, dict):
        info = {}
    yahoo = {key: value for key, value in info.items() if key in fields}
    yahoo.setdefault("symbol", symbol)
    try:
        official = download_sec_fundamental_snapshot(symbol)
    except (ExternalDataError, ValueError) as exc:
        official = {}
        warnings.append(f"SEC EDGAR no pudo validar {symbol}: {exc}")
    result = merge_fundamental_sources(yahoo, official)
    if warnings:
        result["_warnings"] = warnings
    return result


def default_date_range(years: int = 5) -> tuple[date, date]:
    end = date.today()
    return end - timedelta(days=365 * years), end
