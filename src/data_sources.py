"""Fuentes externas complementarias y trazabilidad de los datos.

Yahoo sigue siendo la fuente práctica de precios del prototipo. Este módulo
añade fuentes oficiales cuando existen y una comprobación opcional de precios.
Todas las funciones fallan de forma recuperable para no dejar inutilizable la
aplicación cuando un proveedor no responde.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
import os
from typing import Any, Iterable
from xml.etree import ElementTree

import pandas as pd
import requests


SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ECB_DAILY_FX_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"
DEFAULT_SEC_USER_AGENT = os.getenv(
    "STOCK_SIGNAL_LAB_SEC_USER_AGENT",
    "StockSignalLab/1.0 local-educational-research",
)


class ExternalDataError(RuntimeError):
    """Error recuperable al consultar una fuente complementaria."""


@dataclass(frozen=True)
class PriceVerification:
    ticker: str
    provider: str
    as_of: date
    close: float
    difference_pct: float | None = None
    status: str = "Sin comparar"


@dataclass(frozen=True)
class FxSnapshot:
    as_of: date | None
    rates_per_eur: dict[str, float]
    source: str = "Banco Central Europeo"

    def supports(self, currency: str) -> bool:
        return currency.upper() in self.rates_per_eur


def _get_json(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ExternalDataError(f"No se pudo consultar {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExternalDataError(f"La fuente devolvió un formato inesperado: {url}")
    return payload


def _sec_headers(user_agent: str | None = None) -> dict[str, str]:
    return {
        "User-Agent": (user_agent or DEFAULT_SEC_USER_AGENT).strip(),
        "Accept-Encoding": "gzip, deflate",
        "Host": "www.sec.gov",
    }


@lru_cache(maxsize=2)
def _sec_ticker_map(user_agent: str = DEFAULT_SEC_USER_AGENT) -> dict[str, int]:
    payload = _get_json(SEC_TICKERS_URL, headers=_sec_headers(user_agent))
    mapping: dict[str, int] = {}
    for record in payload.values():
        if not isinstance(record, dict):
            continue
        symbol = str(record.get("ticker") or "").upper()
        cik = record.get("cik_str")
        if symbol and cik is not None:
            mapping[symbol] = int(cik)
    return mapping


def _fact_items(
    company_facts: dict[str, Any],
    concepts: Iterable[str],
) -> list[dict[str, Any]]:
    namespaces = company_facts.get("facts")
    if not isinstance(namespaces, dict):
        return []
    for namespace_name in ("us-gaap", "ifrs-full"):
        namespace = namespaces.get(namespace_name)
        if not isinstance(namespace, dict):
            continue
        for concept in concepts:
            fact = namespace.get(concept)
            if not isinstance(fact, dict):
                continue
            units = fact.get("units")
            if not isinstance(units, dict):
                continue
            preferred_units = ("USD", "EUR", "GBP", "JPY", "CHF", "shares", "USD/shares")
            for unit in (*preferred_units, *units.keys()):
                values = units.get(unit)
                if isinstance(values, list) and values:
                    return [item for item in values if isinstance(item, dict)]
    return []


def _deduplicated_series(
    items: list[dict[str, Any]],
    *,
    annual_flow: bool,
    annual_only: bool = False,
) -> list[dict[str, Any]]:
    accepted_forms = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
    if not annual_only:
        accepted_forms |= {"10-Q", "10-Q/A", "6-K", "6-K/A"}
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if item.get("form") not in accepted_forms or item.get("val") is None or not item.get("end"):
            continue
        if annual_flow:
            if not item.get("start"):
                continue
            duration = (pd.Timestamp(item["end"]) - pd.Timestamp(item["start"])).days
            if not 270 <= duration <= 450:
                continue
            if item.get("form", "").startswith(("10-K", "20-F", "40-F")) is False:
                continue
        cleaned.append(item)
    # Una cuenta comparativa puede aparecer en varios informes. Conservamos la
    # presentación más reciente para cada cierre del periodo.
    cleaned.sort(key=lambda item: (str(item.get("end")), str(item.get("filed", ""))))
    by_end: dict[str, dict[str, Any]] = {}
    for item in cleaned:
        by_end[str(item["end"])] = item
    return [by_end[key] for key in sorted(by_end)]


def _annual_series(company_facts: dict[str, Any], concepts: Iterable[str]) -> list[dict[str, Any]]:
    return _deduplicated_series(
        _fact_items(company_facts, concepts),
        annual_flow=True,
        annual_only=True,
    )


def _instant_series(
    company_facts: dict[str, Any],
    concepts: Iterable[str],
    *,
    annual_only: bool = False,
) -> list[dict[str, Any]]:
    return _deduplicated_series(
        _fact_items(company_facts, concepts),
        annual_flow=False,
        annual_only=annual_only,
    )


def _latest_value(series: list[dict[str, Any]]) -> float | None:
    if not series:
        return None
    try:
        return float(series[-1]["val"])
    except (TypeError, ValueError, KeyError):
        return None


def _growth(series: list[dict[str, Any]]) -> float | None:
    if len(series) < 2:
        return None
    current = _latest_value(series)
    try:
        previous = float(series[-2]["val"])
    except (TypeError, ValueError, KeyError):
        return None
    if current is None or previous == 0:
        return None
    return current / abs(previous) - 1.0


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def download_sec_fundamental_snapshot(
    ticker: str,
    *,
    user_agent: str | None = None,
) -> dict[str, object]:
    """Calcula métricas comparables desde estados financieros oficiales SEC.

    Sólo se consultan tickers sin sufijo de bolsa. Así se evita confundir, por
    ejemplo, una acción europea ``ABC.MC`` con una empresa estadounidense ABC.
    """

    symbol = ticker.strip().upper()
    if not symbol or "." in symbol:
        return {}
    resolved_user_agent = (user_agent or DEFAULT_SEC_USER_AGENT).strip()
    cik = _sec_ticker_map(resolved_user_agent).get(symbol)
    if cik is None:
        return {}
    url = SEC_COMPANY_FACTS_URL.format(cik=cik)
    headers = _sec_headers(resolved_user_agent)
    headers["Host"] = "data.sec.gov"
    company_facts = _get_json(url, headers=headers)

    revenue = _annual_series(
        company_facts,
        (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "Revenues",
            "Revenue",
        ),
    )
    net_income = _annual_series(company_facts, ("NetIncomeLoss", "ProfitLoss"))
    operating_income = _annual_series(
        company_facts,
        ("OperatingIncomeLoss", "ProfitLossFromOperatingActivities"),
    )
    equity_annual = _instant_series(
        company_facts,
        (
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "StockholdersEquity",
            "Equity",
        ),
        annual_only=True,
    )
    equity_latest = _instant_series(
        company_facts,
        (
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "StockholdersEquity",
            "Equity",
        ),
    )
    current_assets = _instant_series(company_facts, ("AssetsCurrent", "CurrentAssets"))
    current_liabilities = _instant_series(
        company_facts,
        ("LiabilitiesCurrent", "CurrentLiabilities"),
    )
    debt_current = _instant_series(
        company_facts,
        (
            "LongTermDebtAndFinanceLeaseObligationsCurrent",
            "LongTermDebtCurrent",
            "CurrentBorrowings",
        ),
    )
    debt_noncurrent = _instant_series(
        company_facts,
        (
            "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
            "LongTermDebtNoncurrent",
            "NoncurrentBorrowings",
        ),
    )
    operating_cash = _annual_series(
        company_facts,
        (
            "NetCashProvidedByUsedInOperatingActivities",
            "CashFlowsFromUsedInOperatingActivities",
        ),
    )
    capex = _annual_series(
        company_facts,
        (
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PurchaseOfPropertyPlantAndEquipment",
        ),
    )

    latest_revenue = _latest_value(revenue)
    latest_income = _latest_value(net_income)
    latest_operating_income = _latest_value(operating_income)
    latest_equity = _latest_value(equity_latest)
    annual_equities = [
        float(item["val"])
        for item in equity_annual[-2:]
        if item.get("val") not in (None, 0)
    ]
    average_equity = (
        sum(annual_equities) / len(annual_equities) if annual_equities else latest_equity
    )
    current_debt = _latest_value(debt_current) or 0.0
    noncurrent_debt = _latest_value(debt_noncurrent) or 0.0
    cash_from_operations = _latest_value(operating_cash)
    capital_expenditure = _latest_value(capex)

    metrics: dict[str, object] = {
        "symbol": symbol,
        "longName": company_facts.get("entityName"),
        "returnOnEquity": _safe_ratio(latest_income, average_equity),
        "profitMargins": _safe_ratio(latest_income, latest_revenue),
        "operatingMargins": _safe_ratio(latest_operating_income, latest_revenue),
        "revenueGrowth": _growth(revenue),
        "earningsGrowth": _growth(net_income),
        "debtToEquity": (
            _safe_ratio(current_debt + noncurrent_debt, latest_equity) * 100
            if latest_equity not in (None, 0)
            else None
        ),
        "currentRatio": _safe_ratio(
            _latest_value(current_assets),
            _latest_value(current_liabilities),
        ),
        "freeCashflow": (
            cash_from_operations - abs(capital_expenditure)
            if cash_from_operations is not None and capital_expenditure is not None
            else None
        ),
        "_official_period_end": max(
            (
                str(series[-1]["end"])
                for series in (
                    revenue,
                    net_income,
                    equity_latest,
                    current_assets,
                    current_liabilities,
                    operating_cash,
                )
                if series
            ),
            default=None,
        ),
        "_official_source": "SEC EDGAR",
        "_official_url": f"https://www.sec.gov/edgar/browse/?CIK={cik}",
    }
    return {key: value for key, value in metrics.items() if value is not None}


def merge_fundamental_sources(
    yahoo: dict[str, object],
    official: dict[str, object] | None,
) -> dict[str, object]:
    """Combina contexto de Yahoo con cifras contables oficiales.

    Los valores SEC prevalecen en las métricas derivadas de cuentas publicadas.
    Yahoo conserva sector, país, moneda de cotización y métricas de valoración.
    """

    result = dict(yahoo)
    sources = {
        key: "Yahoo Finance"
        for key, value in yahoo.items()
        if value is not None and not key.startswith("_")
    }
    official = official or {}
    official_fields = {
        "returnOnEquity",
        "profitMargins",
        "operatingMargins",
        "revenueGrowth",
        "earningsGrowth",
        "debtToEquity",
        "currentRatio",
        "freeCashflow",
    }
    for key in official_fields:
        value = official.get(key)
        if value is not None:
            result[key] = value
            sources[key] = str(official.get("_official_source") or "Fuente oficial")
    if not result.get("longName") and official.get("longName"):
        result["longName"] = official["longName"]
        sources["longName"] = str(official.get("_official_source") or "Fuente oficial")
    result["_sources"] = sources
    result["_providers"] = sorted(set(sources.values()))
    if official.get("_official_period_end"):
        result["_official_period_end"] = official["_official_period_end"]
    if official.get("_official_url"):
        result["_official_url"] = official["_official_url"]
    return result


def download_alpha_vantage_latest_close(ticker: str, api_key: str) -> PriceVerification:
    """Obtiene un cierre diario alternativo para comprobar el dato principal."""

    if not api_key.strip():
        raise ValueError("La clave de Alpha Vantage está vacía.")
    try:
        response = requests.get(
            ALPHA_VANTAGE_URL,
            params={
                "function": "TIME_SERIES_DAILY",
                "symbol": ticker.strip().upper(),
                "outputsize": "compact",
                "apikey": api_key.strip(),
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ExternalDataError(f"Alpha Vantage no respondió para {ticker}: {exc}") from exc
    series = payload.get("Time Series (Daily)") if isinstance(payload, dict) else None
    if not isinstance(series, dict) or not series:
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("Information") or payload.get("Note") or payload.get("Error Message") or "")
        raise ExternalDataError(
            f"Alpha Vantage no devolvió un precio para {ticker}."
            + (f" {message}" if message else "")
        )
    latest_day = max(series)
    try:
        close = float(series[latest_day]["4. close"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ExternalDataError(f"Precio alternativo inválido para {ticker}.") from exc
    return PriceVerification(
        ticker=ticker.strip().upper(),
        provider="Alpha Vantage",
        as_of=pd.Timestamp(latest_day).date(),
        close=close,
    )


def compare_verified_price(
    primary: pd.DataFrame,
    verification: PriceVerification,
    *,
    tolerance_pct: float = 1.0,
) -> PriceVerification:
    """Compara el cierre alternativo con la misma fecha del histórico principal."""

    normalized = primary.copy()
    normalized.index = pd.to_datetime(normalized.index).tz_localize(None).normalize()
    timestamp = pd.Timestamp(verification.as_of)
    if timestamp not in normalized.index:
        return PriceVerification(
            **{
                **verification.__dict__,
                "status": "Fecha no comparable",
            }
        )
    primary_close = float(normalized.loc[timestamp, "close"])
    difference = (verification.close / primary_close - 1.0) * 100.0
    status = "Coincide" if abs(difference) <= tolerance_pct else "Revisar diferencia"
    return PriceVerification(
        ticker=verification.ticker,
        provider=verification.provider,
        as_of=verification.as_of,
        close=verification.close,
        difference_pct=difference,
        status=status,
    )


def download_ecb_fx_snapshot() -> FxSnapshot:
    """Descarga los últimos tipos de referencia del BCE (unidades por euro)."""

    try:
        response = requests.get(ECB_DAILY_FX_URL, timeout=20)
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
    except (requests.RequestException, ElementTree.ParseError) as exc:
        raise ExternalDataError(f"No se pudieron obtener divisas del BCE: {exc}") from exc
    rates: dict[str, float] = {"EUR": 1.0}
    as_of: date | None = None
    for element in root.iter():
        time_value = element.attrib.get("time")
        if time_value:
            as_of = pd.Timestamp(time_value).date()
        currency = element.attrib.get("currency")
        rate = element.attrib.get("rate")
        if currency and rate:
            try:
                rates[currency.upper()] = float(rate)
            except ValueError:
                continue
    if len(rates) == 1:
        raise ExternalDataError("El BCE devolvió una tabla de divisas vacía.")
    return FxSnapshot(as_of=as_of, rates_per_eur=rates)


def convert_currency(
    amount: float,
    from_currency: str,
    to_currency: str,
    rates_per_eur: dict[str, float],
) -> float:
    """Convierte usando tipos expresados como unidades de moneda por un euro."""

    source = from_currency.upper()
    target = to_currency.upper()
    if source == target:
        return amount
    if source not in rates_per_eur or target not in rates_per_eur:
        raise ValueError(f"No hay tipo BCE para convertir {source} a {target}.")
    return amount / rates_per_eur[source] * rates_per_eur[target]


def benchmark_for_ticker(ticker: str) -> str:
    """Elige un índice amplio aproximado según el sufijo de cotización."""

    symbol = ticker.upper()
    suffixes = {
        ".MC": "^IBEX",
        ".L": "^FTSE",
        ".DE": "^GDAXI",
        ".PA": "^FCHI",
        ".AS": "^AEX",
        ".MI": "FTSEMIB.MI",
        ".SW": "^SSMI",
        ".TO": "^GSPTSE",
        ".AX": "^AXJO",
        ".HK": "^HSI",
    }
    return next((benchmark for suffix, benchmark in suffixes.items() if symbol.endswith(suffix)), "SPY")


def sector_benchmark(sector: str | None, ticker: str) -> str | None:
    """Devuelve una referencia sectorial compatible con la plaza de cotización."""

    symbol = ticker.upper()
    # Referencias UCITS negociadas en Londres. Comparar en la misma plaza reduce
    # el ruido de divisa que introduciría enfrentar directamente una acción en
    # GBP con un ETF estadounidense en USD.
    london_mapping = {
        "Consumer Defensive": "ESIS.L",
    }
    if symbol.endswith(".L"):
        return london_mapping.get(sector or "")
    if "." in symbol:
        return None
    mapping = {
        "Technology": "XLK",
        "Healthcare": "XLV",
        "Financial Services": "XLF",
        "Consumer Cyclical": "XLY",
        "Consumer Defensive": "XLP",
        "Industrials": "XLI",
        "Energy": "XLE",
        "Basic Materials": "XLB",
        "Real Estate": "XLRE",
        "Utilities": "XLU",
        "Communication Services": "XLC",
    }
    return mapping.get(sector or "")
