"""Price provenance must survive real indicator preparation, with no I/O."""

from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from config import StrategyConfig
from src.entry_data_gate import normalized_quote_currency_verified
from src.hourly_market_schedule import validated_completed_daily_prices
from src.indicators import add_indicators
from src.price_units import normalize_price_frame_units
from src.signal_engine import add_signal_columns


NOW = "2026-11-23T12:00:00Z"


def raw_prices(currency="USD"):
    index = pd.bdate_range(end="2026-11-20", periods=300)
    close = np.linspace(100, 125, len(index)) + np.sin(np.arange(len(index)))
    data = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                         "close": close, "volume": 1000}, index=index)
    data.attrs.update(provider="synthetic-provider", auto_adjust=True,
                      adjustment_status="provider_confirmed",
                      daily_session_contract="provider_date_labels",
                      provider_attempts=[{"provider": "synthetic", "code": "accepted"}])
    if currency is not None:
        data.attrs["quote_currency"] = currency
    return data


@pytest.mark.parametrize("ticker,currency", [("AAPL", "USD"), ("IBE.MC", "EUR"), ("TEST.L", "GBp")])
def test_indicator_join_preserves_verified_price_units_provenance_and_calendar(ticker, currency):
    source = validated_completed_daily_prices(ticker, raw_prices(currency), now=NOW, require_currency=True)
    original = source.copy(deep=True)
    expected_attrs = deepcopy(source.attrs)
    assert normalized_quote_currency_verified(source)

    indicators = add_indicators(source, StrategyConfig())
    prepared = add_signal_columns(indicators, StrategyConfig())

    assert indicators.attrs == expected_attrs
    assert prepared.attrs == expected_attrs
    assert normalized_quote_currency_verified(prepared)
    pd.testing.assert_frame_equal(source, original)
    pd.testing.assert_frame_equal(prepared.loc[:, source.columns], source)
    # A later analysis may annotate its copy, not mutate the source's evidence.
    prepared.attrs["provider_attempts"][0]["code"] = "changed-only-in-result"
    assert source.attrs == expected_attrs
    assert indicators.attrs == expected_attrs


@pytest.mark.parametrize("ticker,currency,hint,display", [
    ("AAPL", "USD", "USD", "USD"),
    ("IBE.MC", "EUR", "EUR", "EUR"),
    ("TEST.L", "GBp", "GBP", "GBP"),
])
def test_real_prepare_data_retains_source_verification_without_reconverting_units(ticker, currency, hint, display):
    # Exercise the public preparation path; no provider, journal, mail or cache.
    import app

    source = validated_completed_daily_prices(ticker, raw_prices(currency), now=NOW, require_currency=True)
    expected_attrs = deepcopy(source.attrs)
    prepared, summary, *_ = app.prepare_data(
        {ticker: source}, {ticker: {"currency": hint}}, {}, StrategyConfig(),
        align_reference_dates=True,
    )

    assert ticker in prepared
    result = prepared[ticker]
    assert normalized_quote_currency_verified(result)
    assert result.attrs == expected_attrs
    assert result.attrs["display_currency"] == display
    pd.testing.assert_series_equal(result["close"], source["close"])
    assert summary[0]["Cierre"] == source.close.iloc[-1]
    assert source.attrs == expected_attrs


def test_indicator_preparation_does_not_invent_currency_verification():
    source = normalize_price_frame_units(raw_prices(None), None)
    prepared = add_signal_columns(add_indicators(source, StrategyConfig()), StrategyConfig())

    assert source.attrs["quote_units_normalized"] is False
    assert not normalized_quote_currency_verified(prepared)
    assert prepared.attrs == source.attrs
    with pytest.raises(ValueError) as failed:
        normalize_price_frame_units(prepared, None, require_verified=True)
    assert failed.value.code == "currency_unverified"


def test_price_metadata_does_not_change_indicator_or_signal_values():
    source = raw_prices()
    plain = source.copy(deep=True)
    plain.attrs = {}
    with_metadata = add_signal_columns(add_indicators(source, StrategyConfig()), StrategyConfig())
    without_metadata = add_signal_columns(add_indicators(plain, StrategyConfig()), StrategyConfig())

    pd.testing.assert_frame_equal(with_metadata, without_metadata)
