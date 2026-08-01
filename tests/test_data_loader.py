from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src import data_loader


@dataclass
class FakeResponse:
    text: str

    def raise_for_status(self) -> None:
        return None


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


def test_stooq_symbol_translates_common_markets() -> None:
    assert data_loader._stooq_symbol("AAPL") == "AAPL.US"
    assert data_loader._stooq_symbol("SAN.MC") == "SAN.ES"
    assert data_loader._stooq_symbol("^GSPC") == "^SPX"


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
