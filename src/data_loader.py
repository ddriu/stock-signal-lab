"""Descarga y normalización de precios históricos."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from io import StringIO
from urllib.parse import quote

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
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
STOOQ_SUFFIXES = {
    "AS": "NL",
    "L": "UK",
    "MC": "ES",
    "MI": "IT",
    "PA": "FR",
    "SW": "CH",
    "T": "JP",
    "TO": "CA",
}
STOOQ_INDEX_SYMBOLS = {
    "^DJI": "^DJI",
    "^GSPC": "^SPX",
    "^IXIC": "^NDQ",
}

# Nombres y abreviaturas habituales de algunos brókeres no siempre coinciden
# con el símbolo que utilizan las fuentes gratuitas. La app conserva el nombre
# original en la cartera, pero usa esta cotización principal para el análisis.
ANALYSIS_TICKER_ALIASES = {
    "6VO": "RDDT",
    "AMAZON": "AMZN",
    "AMAZON.COM": "AMZN",
    "AMZ": "AMZN",
    # Trade Republic/Revolut pueden mostrar sólo el símbolo de Xetra. Yahoo
    # necesita el sufijo de mercado para distinguir esta cotización en euros.
    "CEBS": "CEBS.DE",
    "NETFLIX": "NFLX",
    "ORACLE": "ORCL",
    "REDDIT": "RDDT",
    "SERVICE NOW": "NOW",
    "SERVICENOW": "NOW",
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


def search_result_market_group(result: object) -> str:
    """Obtiene el mercado incluso para resultados conservados por una sesión antigua."""

    explicit = str(getattr(result, "market_group", "") or "").strip()
    if explicit:
        return explicit
    country = str(getattr(result, "country", "") or "").strip()
    exchange = str(getattr(result, "exchange", "") or "").strip()
    return country or exchange or "Otros mercados"


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
        ("diageo", "diageo plc", "dge", "dge.l"),
        TickerSearchResult(
            ticker="DGE.L",
            name="Diageo plc",
            exchange="London Stock Exchange",
            instrument_type="Acción",
            country="Reino Unido",
            currency="GBP",
            listing_type="Acción local",
        ),
    ),
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
    (
        ("bae systems", "bae systems plc", "british aerospace", "ba.l", "ba.ln"),
        TickerSearchResult(
            ticker="BA.L",
            name="BAE Systems plc",
            exchange="London Stock Exchange",
            instrument_type="Acción",
            country="Reino Unido",
            currency="GBP",
            listing_type="Acción local",
        ),
    ),
    (
        ("bae systems", "bae systems plc", "british aerospace", "baesy"),
        TickerSearchResult(
            ticker="BAESY",
            name="BAE Systems plc",
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


def resolve_analysis_ticker(ticker: str) -> str:
    """Convierte nombres/abreviaturas de bróker en un ticker analizable."""

    value = ticker.strip().upper()
    if not value:
        raise ValueError("El ticker no puede estar vacío.")
    return ANALYSIS_TICKER_ALIASES.get(value, value)


def normalize_ticker(ticker: str) -> str:
    """Normaliza y resuelve aliases conocidos antes de consultar proveedores."""

    return resolve_analysis_ticker(ticker)


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


def _download_yahoo_chart_prices(
    symbol: str,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    *,
    auto_adjust: bool,
) -> pd.DataFrame:
    """Consulta el endpoint de gráficos de Yahoo sin depender de cookies.

    ``yfinance.download`` es la vía principal. Esta segunda ruta resulta útil
    cuando la negociación de cookies de Yahoo falla, algo más frecuente en
    servidores compartidos, y conserva la cotización solicitada sin sustituirla
    silenciosamente por un ADR en otra moneda.
    """

    start_utc = start_ts.tz_localize("UTC")
    end_utc = (end_ts + timedelta(days=1)).tz_localize("UTC")
    try:
        response = requests.get(
            YAHOO_CHART_URL.format(symbol=quote(symbol, safe="")),
            params={
                "period1": int(start_utc.timestamp()),
                "period2": int(end_utc.timestamp()),
                "interval": "1d",
                "events": "div,splits,capitalGains",
                "includeAdjustedClose": "true",
            },
            headers={"User-Agent": "Mozilla/5.0 stock-signal-lab/1.0"},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        chart = payload.get("chart") if isinstance(payload, dict) else None
        result = (chart or {}).get("result")
        if not isinstance(result, list) or not result:
            description = ((chart or {}).get("error") or {}).get("description")
            raise ValueError(description or "respuesta sin series de precios")
        series = result[0]
        metadata = series.get("meta") if isinstance(series.get("meta"), dict) else {}
        timestamps = series.get("timestamp") or []
        indicators = series.get("indicators") or {}
        quotes = indicators.get("quote") or []
        if not timestamps or not quotes:
            raise ValueError("respuesta sin sesiones de mercado")
        quote_values = quotes[0]
        frame = pd.DataFrame(
            {
                "Date": pd.to_datetime(timestamps, unit="s", utc=True).tz_localize(None),
                "Open": quote_values.get("open"),
                "High": quote_values.get("high"),
                "Low": quote_values.get("low"),
                "Close": quote_values.get("close"),
                "Volume": quote_values.get("volume"),
            }
        ).set_index("Date")

        adjusted = indicators.get("adjclose") or []
        adjusted_values = adjusted[0].get("adjclose") if adjusted else None
        if auto_adjust and adjusted_values is not None:
            adjusted_close = pd.Series(adjusted_values, index=frame.index, dtype=float)
            ratio = adjusted_close.div(frame["Close"]).replace([float("inf"), float("-inf")], pd.NA)
            ratio = ratio.fillna(1.0)
            for column in ("Open", "High", "Low", "Close"):
                frame[column] = pd.to_numeric(frame[column], errors="coerce") * ratio
        frame.attrs["provider"] = "Yahoo Finance (conexión directa)"
        frame.attrs["quote_currency"] = str(metadata.get("currency") or "").strip()
        return frame
    except (requests.RequestException, AttributeError, KeyError, TypeError, ValueError) as exc:
        # No se expone la URL completa ni la respuesta interna del proveedor en
        # la interfaz móvil. El detalle técnico sigue encadenado en la excepción.
        raise DataDownloadError(
            f"Yahoo directo no tiene una serie diaria utilizable para {symbol}."
        ) from exc


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
    except Exception:  # yfinance puede lanzar errores de red heterogéneos
        frame = pd.DataFrame()

    if frame.empty:
        try:
            frame = _download_yahoo_chart_prices(
                symbol,
                start_ts,
                end_ts,
                auto_adjust=auto_adjust,
            )
        except DataDownloadError:
            # Se intenta Stooq a continuación; el detalle del proveedor no es
            # útil para quien usa la aplicación desde el móvil.
            pass

    if frame.empty:
        try:
            frame = _download_stooq_prices(symbol, start_ts, end_ts)
        except DataDownloadError as exc:
            raise DataDownloadError(
                f"No encontramos precios para {symbol} en ese periodo. "
                "Busca la empresa por nombre o comprueba el ticker y su mercado."
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
    # Se conserva el instante real de la consulta dentro del propio DataFrame.
    # Así la interfaz puede distinguir una descarga nueva de un objeto devuelto
    # por la caché de Streamlit.
    result.attrs["fetched_at_utc"] = pd.Timestamp.now(tz="UTC").isoformat()
    if frame.attrs.get("quote_currency"):
        result.attrs["quote_currency"] = frame.attrs["quote_currency"]
    return result


def _statement_series(
    frame: pd.DataFrame | None,
    labels: tuple[str, ...],
) -> list[float]:
    """Extrae una serie anual de una tabla contable de yfinance."""

    if frame is None or frame.empty:
        return []
    for label in labels:
        if label not in frame.index:
            continue
        row = frame.loc[label]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        values = pd.to_numeric(row, errors="coerce").dropna()
        if values.empty:
            return []
        try:
            values = values.sort_index(ascending=False)
        except TypeError:
            pass
        return [float(value) for value in values.tolist()]
    return []


def _statement_growth(values: list[float]) -> float | None:
    if len(values) < 2 or values[1] == 0:
        return None
    return values[0] / abs(values[1]) - 1.0


def _yahoo_statement_snapshot(stock: object) -> dict[str, object]:
    """Calcula un respaldo contable cuando ``get_info`` llega incompleto.

    Yahoo mantiene endpoints separados para ficha e informes financieros. En
    ocasiones la ficha falla en Streamlit Cloud mientras las tablas anuales sí
    responden; aprovecharlas recupera crecimiento, márgenes, balance y caja sin
    inventar datos.
    """

    income = stock.get_income_stmt(freq="yearly")
    balance = stock.get_balance_sheet(freq="yearly")
    cashflow = stock.get_cash_flow(freq="yearly")

    revenue = _statement_series(income, ("TotalRevenue", "OperatingRevenue"))
    net_income = _statement_series(
        income,
        ("NetIncome", "NetIncomeCommonStockholders"),
    )
    operating_income = _statement_series(income, ("OperatingIncome",))
    equity = _statement_series(
        balance,
        ("StockholdersEquity", "CommonStockEquity", "TotalEquityGrossMinorityInterest"),
    )
    current_assets = _statement_series(balance, ("CurrentAssets", "TotalCurrentAssets"))
    current_liabilities = _statement_series(
        balance,
        ("CurrentLiabilities", "TotalCurrentLiabilities"),
    )
    debt = _statement_series(balance, ("TotalDebt",))
    free_cashflow = _statement_series(cashflow, ("FreeCashFlow",))
    operating_cash = _statement_series(
        cashflow,
        ("OperatingCashFlow", "TotalCashFromOperatingActivities"),
    )
    capex = _statement_series(cashflow, ("CapitalExpenditure", "CapitalExpenditures"))

    latest_revenue = revenue[0] if revenue else None
    latest_income = net_income[0] if net_income else None
    latest_operating = operating_income[0] if operating_income else None
    average_equity = (
        sum(equity[:2]) / len(equity[:2]) if equity else None
    )
    latest_fcf = free_cashflow[0] if free_cashflow else None
    if latest_fcf is None and operating_cash:
        latest_fcf = operating_cash[0] - abs(capex[0]) if capex else operating_cash[0]

    def ratio(numerator: float | None, denominator: float | None) -> float | None:
        if numerator is None or denominator in (None, 0):
            return None
        return numerator / denominator

    metrics: dict[str, object | None] = {
        "revenueGrowth": _statement_growth(revenue),
        "earningsGrowth": _statement_growth(net_income),
        "profitMargins": ratio(latest_income, latest_revenue),
        "operatingMargins": ratio(latest_operating, latest_revenue),
        "returnOnEquity": ratio(latest_income, average_equity),
        "currentRatio": ratio(
            current_assets[0] if current_assets else None,
            current_liabilities[0] if current_liabilities else None,
        ),
        "debtToEquity": (
            ratio(debt[0], equity[0]) * 100
            if debt and equity and ratio(debt[0], equity[0]) is not None
            else None
        ),
        "freeCashflow": latest_fcf,
    }
    return {key: value for key, value in metrics.items() if value is not None}


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
        "quoteType",
        "currency",
        "exchange",
        "fullExchangeName",
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
        # Fechas orientativas del próximo evento. Yahoo puede no publicarlas o
        # modificarlas; la capa de oportunidades conserva esa incertidumbre.
        "earningsTimestamp",
        "earningsTimestampStart",
        "earningsTimestampEnd",
        "earningsDate",
        "exDividendDate",
        "dividendDate",
    }
    warnings: list[str] = []
    stock = yf.Ticker(symbol)
    try:
        info = stock.get_info()
    except Exception as exc:
        info = {}
        warnings.append(f"Yahoo no pudo aportar fundamentales de {symbol}: {exc}")
    if not isinstance(info, dict):
        info = {}
    yahoo = {key: value for key, value in info.items() if key in fields}
    yahoo.setdefault("symbol", symbol)
    accounting_fields = {
        "returnOnEquity",
        "profitMargins",
        "operatingMargins",
        "revenueGrowth",
        "earningsGrowth",
        "debtToEquity",
        "currentRatio",
        "freeCashflow",
    }
    if sum(yahoo.get(key) is not None for key in accounting_fields) < 2:
        try:
            statement_metrics = _yahoo_statement_snapshot(stock)
        except Exception as exc:
            statement_metrics = {}
            warnings.append(
                f"Yahoo no pudo aportar estados financieros de {symbol}: {exc}"
            )
        for key, value in statement_metrics.items():
            if yahoo.get(key) is None:
                yahoo[key] = value
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
