"""Provider-to-Radar integration; synthetic responses only, never network."""
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from src import data_loader
from src.hourly_market_schedule import completed_daily_prices


NOW = "2026-11-21T12:57:00Z"


def bars(day="2026-11-20", close=100.0):
    return pd.DataFrame({"open": [close], "high": [close + 1], "low": [close - 1],
                         "close": [close], "volume": [1000.]}, index=pd.DatetimeIndex([day]))


def yahoo_payload(symbol="AAPL", timestamp="2026-11-20T14:30:00Z", zone="America/New_York", granularity="1d"):
    return {"chart": {"result": [{"meta": {"symbol": symbol, "currency": "USD",
        "exchangeTimezoneName": zone, "dataGranularity": granularity},
        "timestamp": [int(pd.Timestamp(timestamp).timestamp())],
        "indicators": {"quote": [{"open": [100.], "high": [101.], "low": [99.],
            "close": [100.], "volume": [1000]}], "adjclose": [{"adjclose": [100.]}]}}]}}


def stub_sources(monkeypatch, *, primary=None, payload=None, stooq=None):
    calls = []
    monkeypatch.setattr(data_loader.yf, "download", lambda *a, **k: primary.copy() if primary is not None else pd.DataFrame())
    def get(url, **kwargs):
        calls.append(url)
        if "stooq" in url:
            return SimpleNamespace(raise_for_status=lambda: None, text=stooq or "No data")
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
    monkeypatch.setattr(data_loader.requests, "get", get)
    return calls


def test_realistic_yahoo_daily_open_timestamp_reaches_completed_radar(monkeypatch):
    calls = stub_sources(monkeypatch, payload=yahoo_payload())
    raw = data_loader.download_prices("AAPL", date(2026, 11, 19), date(2026, 11, 21))
    closed = completed_daily_prices("AAPL", raw, now=NOW)
    assert len(closed) == 1
    assert closed.index[-1] == pd.Timestamp("2026-11-20")
    assert len(calls) == 1


def test_sydney_daily_timestamp_maps_to_next_local_session(monkeypatch):
    stub_sources(monkeypatch, payload=yahoo_payload("BHP.AX", "2026-11-19T23:00:00Z", "Australia/Sydney"))
    raw = data_loader.download_prices("BHP.AX", date(2026, 11, 19), date(2026, 11, 21))
    assert raw.index[-1] == pd.Timestamp("2026-11-20")
    assert len(completed_daily_prices("BHP.AX", raw, now=NOW)) == 1


@pytest.mark.parametrize(("zone", "granularity"), [("", "1d"), ("Invalid/Zone", "1d"),
    ("America/New_York", "5m"), ("Asia/Tokyo", "1d")])
def test_unverified_metadata_is_not_normalized_as_a_verified_daily_series(monkeypatch, zone, granularity):
    stub_sources(monkeypatch, payload=yahoo_payload(zone=zone, granularity=granularity))
    with pytest.raises(data_loader.DataDownloadError):
        data_loader.download_prices("AAPL", date(2026, 11, 19), date(2026, 11, 21))


@pytest.mark.parametrize("problem", ["stale", "invalid", "currency"])
def test_validator_rejects_primary_before_trying_next_provider(monkeypatch, problem):
    from src.hourly_market_schedule import validated_completed_daily_prices
    from src.price_units import normalize_price_frame_units
    primary = bars("2026-11-19") if problem == "stale" else bars(close=0. if problem == "invalid" else 100.)
    if problem == "currency":
        primary.attrs["quote_currency"] = "GBP"
    calls = stub_sources(monkeypatch, primary=primary, payload=yahoo_payload())
    def validate(ticker, frame):
        normalized = normalize_price_frame_units(frame, "USD", require_verified=True)
        return validated_completed_daily_prices(ticker, normalized, now=NOW)
    fresh = data_loader.download_prices("AAPL", date(2026, 11, 19), date(2026, 11, 21),
        validator=validate)
    assert fresh.index[-1] == pd.Timestamp("2026-11-20")
    assert fresh.attrs["provider"] == "Yahoo Finance (conexión directa)"
    assert len(calls) == 1
    assert fresh.attrs["provider_attempts"][0]["code"] == {
        "stale": "stale_close", "invalid": "invalid_ohlc", "currency": "currency_mismatch",
    }[problem]


def test_stooq_fallback_does_not_certify_adjustments_or_currency(monkeypatch):
    stub_sources(monkeypatch, payload=None, stooq="Date,Open,High,Low,Close,Volume\n2026-11-20,100,101,99,100,1000\n")
    raw = data_loader.download_prices("AAPL", date(2026, 11, 19), date(2026, 11, 21))
    assert raw.attrs["provider"] == "Stooq"
    assert raw.attrs["adjustment_status"] == "unverified"
    assert raw.attrs["auto_adjust"] is None
    assert not raw.attrs.get("quote_currency")


def test_all_provider_failures_preserve_stage_codes_without_private_error_bodies(monkeypatch):
    from src.hourly_market_schedule import validated_completed_daily_prices
    calls = stub_sources(monkeypatch, primary=bars("2026-11-19"), payload=None)
    with pytest.raises(data_loader.DataDownloadError) as failed:
        data_loader.download_prices("AAPL", date(2026, 11, 19), date(2026, 11, 21),
            validator=lambda ticker, frame: validated_completed_daily_prices(ticker, frame, now=NOW))
    assert failed.value.code == "stale_close"
    assert failed.value.attempts[0] == {"provider": "Yahoo Finance", "code": "stale_close"}
    assert len(calls) == 2


def test_strict_review_retries_primary_missing_currency_and_uses_declared_fallback(monkeypatch):
    from src.hourly_market_schedule import validated_completed_daily_prices
    calls = stub_sources(monkeypatch, primary=bars(), payload=yahoo_payload())
    fresh = data_loader.download_prices("AAPL", date(2026, 11, 19), date(2026, 11, 21),
        validator=lambda ticker, frame: validated_completed_daily_prices(
            ticker, frame, now=NOW, require_currency=True))
    assert fresh.attrs["provider"] == "Yahoo Finance (conexión directa)"
    assert fresh.attrs["provider_attempts"] == [{"provider": "Yahoo Finance", "code": "currency_unverified"}]
    assert fresh.attrs["display_currency"] == "USD"
    assert fresh.attrs["quote_units_normalized"] is True
    assert len(calls) == 1


def test_explicit_existing_currency_hint_avoids_redundant_provider_fetch(monkeypatch):
    from src.hourly_market_schedule import validated_completed_daily_prices
    calls = stub_sources(monkeypatch, primary=bars(), payload=None)
    fresh = data_loader.download_prices("AAPL", date(2026, 11, 19), date(2026, 11, 21),
        validator=lambda ticker, frame: validated_completed_daily_prices(
            ticker, frame, now=NOW, require_currency=True, currency="USD"))
    assert fresh.attrs["display_currency"] == "USD"
    assert fresh.attrs["provider"] == "Yahoo Finance"
    assert calls == []


def test_metadata_first_strict_review_avoids_primary_request_when_direct_is_valid(monkeypatch):
    from src.hourly_market_schedule import validated_completed_daily_prices
    calls = stub_sources(monkeypatch, payload=yahoo_payload())
    monkeypatch.setattr(data_loader.yf, "download", lambda *a, **k: pytest.fail("No redundant primary request"))
    result = data_loader.download_prices("AAPL", date(2026, 11, 19), date(2026, 11, 21),
        prefer_metadata=True, validator=lambda ticker, frame: validated_completed_daily_prices(
            ticker, frame, now=NOW, require_currency=True))
    assert result.attrs["provider"] == "Yahoo Finance (conexión directa)"
    assert result.attrs["provider_attempts"] == []
    assert len(calls) == 1


def test_metadata_first_invalid_daily_contract_can_fall_back_to_valid_primary(monkeypatch):
    from src.hourly_market_schedule import validated_completed_daily_prices
    primary = bars()
    primary.attrs["quote_currency"] = "USD"
    calls = stub_sources(monkeypatch, primary=primary, payload=yahoo_payload(granularity="5m"))
    result = data_loader.download_prices("AAPL", date(2026, 11, 19), date(2026, 11, 21),
        prefer_metadata=True, validator=lambda ticker, frame: validated_completed_daily_prices(
            ticker, frame, now=NOW, require_currency=True))
    assert result.attrs["provider"] == "Yahoo Finance"
    assert result.attrs["provider_attempts"] == [{
        "provider": "Yahoo Finance (conexión directa)", "code": "unverified_session_labels"}]
    assert len(calls) == 1
