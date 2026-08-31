from datetime import date

import pandas as pd
import pytest

from src.data_sources import (
    PriceVerification,
    benchmark_for_ticker,
    compare_verified_price,
    convert_currency,
    merge_fundamental_sources,
    sector_benchmark,
)


def test_official_fundamentals_override_yahoo_accounting_fields() -> None:
    result = merge_fundamental_sources(
        {
            "symbol": "TEST",
            "sector": "Technology",
            "returnOnEquity": 0.10,
            "forwardPE": 20,
        },
        {
            "returnOnEquity": 0.25,
            "totalCash": 2_000_000,
            "operatingCashflow": 500_000,
            "capitalExpenditures": 100_000,
            "_official_source": "SEC EDGAR",
            "_official_period_end": "2025-12-31",
        },
    )
    assert result["returnOnEquity"] == 0.25
    assert result["totalCash"] == 2_000_000
    assert result["operatingCashflow"] == 500_000
    assert result["capitalExpenditures"] == 100_000
    assert result["forwardPE"] == 20
    assert result["_sources"]["returnOnEquity"] == "SEC EDGAR"
    assert result["_sources"]["forwardPE"] == "Yahoo Finance"


def test_currency_conversion_uses_units_per_euro() -> None:
    rates = {"EUR": 1.0, "USD": 1.2, "GBP": 0.8}
    assert convert_currency(100, "EUR", "USD", rates) == pytest.approx(120)
    assert convert_currency(120, "USD", "GBP", rates) == pytest.approx(80)


def test_price_verification_compares_same_date() -> None:
    frame = pd.DataFrame(
        {"close": [100.0]},
        index=pd.to_datetime(["2026-01-02"]),
    )
    checked = compare_verified_price(
        frame,
        PriceVerification(
            ticker="TEST",
            provider="Alternative",
            as_of=date(2026, 1, 2),
            close=100.5,
        ),
    )
    assert checked.difference_pct == pytest.approx(0.5)
    assert checked.status == "Coincide"


def test_london_consumer_company_uses_local_market_and_sector_references() -> None:
    assert benchmark_for_ticker("DGE.L") == "^FTSE"
    assert sector_benchmark("Consumer Defensive", "DGE.L") == "ESIS.L"
