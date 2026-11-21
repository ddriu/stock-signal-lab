from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd
import pytest

from src.entry_data_gate import verified_entry_prices
from src.radar_review import RadarOutcome, RadarReview


NOW = datetime(2026, 11, 21, 13, tzinfo=timezone.utc)
IDENTITY = "a" * 64


def prices(*dates, value=100.):
    result = pd.DataFrame({"open": value, "high": value + 1, "low": value - 1,
        "close": value, "volume": 1000}, index=pd.DatetimeIndex(dates or ["2026-11-18", "2026-11-19", "2026-11-20"]))
    result.attrs.update(quote_units_normalized=True, display_currency="USD")
    return result


def policy(ticker="AAPL", **changes):
    return {"snapshot_id": IDENTITY, "ticker": ticker, "as_of": "2026-11-20", "price": 100., "currency": "USD",
            "primary": {"data_status": "complete"}, "comparative": {"data_status": "complete"},
            "data_quality": {"block_entry": False}, **changes}


def review(outcomes=None, *, universe=None):
    base = RadarReview.start(universe or list(outcomes or {"AAPL": None}), "b" * 64, NOW)
    return replace(base, outcomes=outcomes or {"AAPL": RadarOutcome("complete", "complete", "2026-11-20", IDENTITY, price_verified=True)})


def test_only_exact_complete_saved_and_current_reading_can_feed_entries():
    data = {ticker: prices() for ticker in ("AAPL", "MSFT", "NVDA", "AMD")}
    checked = review({"AAPL": RadarOutcome("complete", "complete", "2026-11-20", IDENTITY, price_verified=True),
                     "MSFT": RadarOutcome("partial", "partial", "2026-11-20", IDENTITY, price_verified=True),
                     "NVDA": RadarOutcome("no_data")}, universe=["AAPL", "MSFT", "NVDA", "AMD"])
    result = verified_entry_prices(data, review=checked, policies={x: policy(x) for x in data}, now=NOW)
    assert list(result.prices) == ["AAPL"]
    assert result.excluded == {"MSFT": "analysis_partial", "NVDA": "review_failed", "AMD": "review_pending"}
    assert result.prices["AAPL"] is data["AAPL"]


def test_failed_refresh_cannot_reuse_a_previous_frame_even_on_same_price_date():
    old = prices()
    result = verified_entry_prices({"AAPL": old}, review=review({"AAPL": RadarOutcome("no_data")}),
                                   policies={"AAPL": policy()}, now=NOW)
    assert not result.prices and result.excluded["AAPL"] == "review_failed"
    assert old.index[-1].date().isoformat() == "2026-11-20"


def test_review_universe_includes_symbols_without_any_cached_frame():
    result = verified_entry_prices({"AAPL": prices()}, review=review(universe=["AAPL", "MSFT", "NVDA", "AMD"]),
                                   policies={"AAPL": policy()}, now=NOW)
    assert list(result.prices) == ["AAPL"]
    assert result.excluded == {"MSFT": "review_pending", "NVDA": "review_pending", "AMD": "review_pending"}


@pytest.mark.parametrize("changed", [
    {"snapshot_id": "c" * 64}, {"ticker": "MSFT"}, {"as_of": "2026-11-19"}, {"price": 99.},
])
def test_memory_must_match_persisted_identity_price_and_day(changed):
    result = verified_entry_prices({"AAPL": prices()}, review=review(), policies={"AAPL": policy(**changed)}, now=NOW)
    assert result.excluded == {"AAPL": "snapshot_mismatch"}


@pytest.mark.parametrize("quality", [None, {}, {"block_entry": True}])
def test_unknown_or_blocked_quality_is_not_a_buy(quality):
    result = verified_entry_prices({"AAPL": prices()}, review=review(),
                                   policies={"AAPL": policy(data_quality=quality)}, now=NOW)
    assert result.excluded == {"AAPL": "analysis_blocked"}


def test_verified_but_stale_reading_is_rejected_by_actual_exchange_close():
    old = prices("2026-11-18", "2026-11-19")
    checked = review({"AAPL": RadarOutcome("complete", "complete", "2026-11-19", IDENTITY, price_verified=True)})
    result = verified_entry_prices({"AAPL": old}, review=checked,
                                   policies={"AAPL": policy(as_of="2026-11-19")}, now=NOW)
    assert result.excluded == {"AAPL": "stale_close"}


@pytest.mark.parametrize("attrs", [{}, {"quote_units_normalized": False},
    {"quote_units_normalized": True, "display_currency": "XXX"}])
def test_legacy_or_unknown_quote_units_are_historical_only(attrs):
    raw = prices()
    raw.attrs = attrs
    result = verified_entry_prices({"AAPL": raw}, review=review(), policies={"AAPL": policy()}, now=NOW)
    assert result.excluded == {"AAPL": "currency_unverified"}


def test_policy_currency_must_match_actual_normalized_price_currency():
    result = verified_entry_prices({"AAPL": prices()}, review=review(),
                                   policies={"AAPL": policy(currency="EUR")}, now=NOW)
    assert result.excluded == {"AAPL": "currency_mismatch"}


def test_ongoing_us_session_bar_is_not_allowed_to_leak_into_indicators():
    result = verified_entry_prices({"AAPL": prices()}, review=review(), policies={"AAPL": policy()},
                                   now="2026-11-20T17:00:00Z")
    assert not result.prices


@pytest.mark.parametrize("field,bad", [("close", 0.), ("low", 102.), ("volume", -1.)])
def test_corrupt_historical_ohlc_cannot_enter_current_opportunities(field, bad):
    raw = prices()
    raw.iloc[0, raw.columns.get_loc(field)] = bad
    result = verified_entry_prices({"AAPL": raw}, review=review(), policies={"AAPL": policy()}, now=NOW)
    assert result.excluded == {"AAPL": "invalid_ohlc"}


def test_old_checkpoint_without_diagnostic_roundtrips_and_unknown_diagnostic_is_rejected():
    checked = review()
    payload = checked.checkpoint()
    payload["outcomes"]["AAPL"].pop("diagnostic_code")
    assert RadarReview.from_checkpoint(payload) == checked
    with pytest.raises(ValueError, match="Diagnóstico"):
        RadarOutcome("no_data", diagnostic_code="private raw provider response")


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None])
def test_quote_verification_does_not_accept_truthy_strings_or_numbers(value):
    with pytest.raises(ValueError, match="Comprobación"):
        RadarOutcome("complete", "complete", "2026-11-20", IDENTITY, price_verified=value)


def test_legacy_manifest_without_price_validation_cannot_feed_entries():
    legacy = review({"AAPL": RadarOutcome("complete", "complete", "2026-11-20", IDENTITY)})
    result = verified_entry_prices({"AAPL": prices()}, review=legacy, policies={"AAPL": policy()}, now=NOW)
    assert result.excluded == {"AAPL": "review_not_verified"}


def test_stale_quote_cannot_claim_current_price_validation():
    with pytest.raises(ValueError, match="Comprobación"):
        RadarOutcome("stale", price_date="2026-11-19", price_verified=True)


def test_diagnostic_code_is_preserved_without_a_provider_response_body():
    checked = review({"AAPL": RadarOutcome("stale", diagnostic_code="stale_close")})
    restored = RadarReview.from_checkpoint(checked.checkpoint())
    assert restored.outcomes["AAPL"].diagnostic_code == "stale_close"
