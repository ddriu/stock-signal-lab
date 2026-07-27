"""Descarga y normalización de precios históricos."""

from __future__ import annotations

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


def normalize_ticker(ticker: str) -> str:
    value = ticker.strip().upper()
    if not value:
        raise ValueError("El ticker no puede estar vacío.")
    return value


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
