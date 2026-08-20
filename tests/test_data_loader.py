from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
import pytest

from src import data_loader


@dataclass
class FakeResponse:
    text: str
    payload: dict[str, Any] | None = None

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        if self.payload is None:
            raise ValueError("No es JSON")
        return self.payload


def test_download_prices_uses_stooq_when_yahoo_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(data_loader.yf, "download", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        data_loader.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            "Date,Open,High,Low,Close,Volume\n"
            "2025-01-02,100,104,99,103,1200000\n"
            "2025-01-03,103,105,101,104,900000\n"
        ),
    )

    frame = data_loader.download_prices("AAPL", "2025-01-01", "2025-01-04")

    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert len(frame) == 2
    assert frame.index.is_monotonic_increasing
    assert frame.attrs["ticker"] == "AAPL"
    assert frame.attrs["provider"] == "Stooq"
    assert pd.notna(pd.to_datetime(frame.attrs["fetched_at_utc"], utc=True))


def test_stooq_symbol_translates_common_markets() -> None:
    assert data_loader._stooq_symbol("AAPL") == "AAPL.US"
    assert data_loader._stooq_symbol("SAN.MC") == "SAN.ES"
    assert data_loader._stooq_symbol("7974.T") == "7974.JP"
    assert data_loader._stooq_symbol("^GSPC") == "^SPX"


def test_broker_aliases_resolve_to_analysis_tickers() -> None:
    assert data_loader.resolve_analysis_ticker("6vo") == "RDDT"
    assert data_loader.resolve_analysis_ticker("AMZ") == "AMZN"
    assert data_loader.resolve_analysis_ticker("CEBS") == "CEBS.DE"
    assert data_loader.resolve_analysis_ticker("Netflix") == "NFLX"
    assert data_loader.resolve_analysis_ticker("ServiceNow") == "NOW"
    assert data_loader.resolve_analysis_ticker("SONY") == "SONY"


def test_download_prices_uses_direct_yahoo_chart_before_stooq(monkeypatch) -> None:
    monkeypatch.setattr(data_loader.yf, "download", lambda *args, **kwargs: pd.DataFrame())
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1735776000, 1735862400],
                    "indicators": {
                        "quote": [
                            {
                                "open": [100.0, 102.0],
                                "high": [104.0, 105.0],
                                "low": [99.0, 101.0],
                                "close": [103.0, 104.0],
                                "volume": [1_200_000, 900_000],
                            }
                        ],
                        "adjclose": [{"adjclose": [101.0, 104.0]}],
                    },
                }
            ],
            "error": None,
        }
    }
    monkeypatch.setattr(
        data_loader.requests,
        "get",
        lambda url, **kwargs: FakeResponse("", payload=payload),
    )

    frame = data_loader.download_prices("7974.T", "2025-01-01", "2025-01-04")

    assert len(frame) == 2
    assert frame.attrs["ticker"] == "7974.T"
    assert frame.attrs["provider"] == "Yahoo Finance (conexión directa)"
    assert frame["close"].tolist() == [101.0, 104.0]


def test_download_error_is_short_and_does_not_expose_provider_url(monkeypatch) -> None:
    monkeypatch.setattr(data_loader.yf, "download", lambda *args, **kwargs: pd.DataFrame())

    def fail_request(*args, **kwargs):
        raise data_loader.requests.HTTPError(
            "404 Client Error: Not Found for url: https://provider.example/very/long/url"
        )

    monkeypatch.setattr(data_loader.requests, "get", fail_request)

    with pytest.raises(data_loader.DataDownloadError) as error:
        data_loader.download_prices("empresa inexistente", "2025-01-01", "2025-02-01")

    message = str(error.value)
    assert "http" not in message
    assert "Busca la empresa por nombre" in message


def test_search_instruments_returns_only_stocks_and_etfs(monkeypatch) -> None:
    class FakeSearch:
        def __init__(self, *args, **kwargs) -> None:
            self.quotes = [
                {
                    "symbol": "TSM",
                    "longname": "Taiwan Semiconductor Manufacturing",
                    "quoteType": "EQUITY",
                    "exchDisp": "NYSE",
                },
                {
                    "symbol": "QQQ",
                    "shortname": "Invesco QQQ Trust",
                    "quoteType": "ETF",
                    "exchange": "NasdaqGM",
                },
                {
                    "symbol": "BTC-USD",
                    "shortname": "Bitcoin USD",
                    "quoteType": "CRYPTOCURRENCY",
                },
            ]

    monkeypatch.setattr(data_loader.yf, "Search", FakeSearch)

    results = data_loader.search_instruments("taiwan")

    assert [result.ticker for result in results] == ["TSM", "QQQ"]
    assert results[0].label == (
        "Taiwan Semiconductor Manufacturing (TSM) · NYSE"
    )
    assert results[0].country == "Estados Unidos"
    assert results[0].currency == "USD"
    assert results[0].details == (
        "Acción · Estados Unidos · USD · Cotización estadounidense"
    )


def test_market_metadata_recognizes_japan_and_international_london() -> None:
    assert data_loader._market_metadata("7974.T", "Tokyo") == (
        "Japón",
        "JPY",
        "Acción local",
    )
    assert data_loader._market_metadata("KAP.IL", "London IOB") == (
        "Londres internacional",
        "USD",
        "GDR internacional",
    )


def test_market_group_supports_results_from_older_sessions() -> None:
    class OldSearchResult:
        country = ""
        exchange = "Tokyo"

    assert data_loader.search_result_market_group(OldSearchResult()) == "Tokyo"
    assert data_loader.search_result_market_group(object()) == "Otros mercados"


def test_curated_international_search_survives_yahoo_failure(monkeypatch) -> None:
    class FailingSearch:
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("Yahoo temporalmente no disponible")

    monkeypatch.setattr(data_loader.yf, "Search", FailingSearch)

    results = data_loader.search_instruments("Nintendo")

    assert [result.ticker for result in results] == ["7974.T", "NTDOY"]
    assert results[0].country == "Japón"
    assert results[1].listing_type == "ADR / OTC"


def test_curated_bae_systems_search_survives_yahoo_failure(monkeypatch) -> None:
    class FailingSearch:
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("Yahoo temporalmente no disponible")

    monkeypatch.setattr(data_loader.yf, "Search", FailingSearch)

    results = data_loader.search_instruments("BAE Systems plc")

    assert [result.ticker for result in results] == ["BA.L", "BAESY"]
    assert results[0].country == "Reino Unido"
    assert results[1].listing_type == "ADR / OTC"


def test_curated_diageo_search_survives_yahoo_failure(monkeypatch) -> None:
    class FailingSearch:
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("Yahoo temporalmente no disponible")

    monkeypatch.setattr(data_loader.yf, "Search", FailingSearch)

    results = data_loader.search_instruments("Diageo plc")

    assert [result.ticker for result in results] == ["DGE.L"]
    assert results[0].country == "Reino Unido"


def test_fundamentals_fall_back_to_yahoo_financial_statements(monkeypatch) -> None:
    columns = pd.to_datetime(["2025-12-31", "2024-12-31"])

    class FakeTicker:
        def get_info(self):
            return {
                "symbol": "TEST",
                "shortName": "Test Company",
                "revenueGrowth": None,
            }

        def get_income_stmt(self, **kwargs):
            return pd.DataFrame(
                {
                    columns[0]: [120.0, 24.0, 30.0],
                    columns[1]: [100.0, 20.0, 25.0],
                },
                index=["TotalRevenue", "NetIncome", "OperatingIncome"],
            )

        def get_balance_sheet(self, **kwargs):
            return pd.DataFrame(
                {
                    columns[0]: [110.0, 80.0, 40.0, 30.0],
                    columns[1]: [90.0, 70.0, 35.0, 28.0],
                },
                index=[
                    "StockholdersEquity",
                    "CurrentAssets",
                    "CurrentLiabilities",
                    "TotalDebt",
                ],
            )

        def get_cash_flow(self, **kwargs):
            return pd.DataFrame(
                {
                    columns[0]: [18.0],
                    columns[1]: [14.0],
                },
                index=["FreeCashFlow"],
            )

    monkeypatch.setattr(data_loader.yf, "Ticker", lambda symbol: FakeTicker())
    monkeypatch.setattr(
        data_loader,
        "download_sec_fundamental_snapshot",
        lambda symbol: {},
    )

    result = data_loader.download_fundamental_snapshot("TEST")

    assert result["revenueGrowth"] == pytest.approx(0.20)
    assert result["earningsGrowth"] == pytest.approx(0.20)
    assert result["operatingMargins"] == pytest.approx(0.25)
    assert result["freeCashflow"] == pytest.approx(18.0)
