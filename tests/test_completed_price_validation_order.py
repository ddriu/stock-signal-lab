"""Completed-session validation precedes OHLC checks of incomplete sessions."""

from types import SimpleNamespace

import exchange_calendars
import numpy as np
import pandas as pd
import pytest

from config import StrategyConfig
from src import data_loader
from src.hourly_market_schedule import validated_completed_daily_prices
from src.indicators import add_indicators
from src.signal_engine import add_signal_columns, evaluate_latest_signal


OPEN_CLOCK = "2026-11-24T17:28:00Z"


def bars(dates=("2026-11-23", "2026-11-24")):
    result = pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100.,
                           "volume": 1000.}, index=pd.DatetimeIndex(dates))
    result.attrs["quote_currency"] = "USD"
    return result


def install_sources(monkeypatch, frame):
    calls = []
    epochs = [int(day.tz_localize("America/New_York").replace(hour=9, minute=30).timestamp())
              for day in frame.index]
    payload = {"chart": {"result": [{"meta": {"symbol": "SYNTH", "currency": "USD",
        "exchangeTimezoneName": "America/New_York", "dataGranularity": "1d"},
        "timestamp": epochs,
        "indicators": {"quote": [{column: frame[column].tolist() for column in frame}],
                       "adjclose": [{"adjclose": frame.close.tolist()}]}}]}}

    def primary(*args, **kwargs):
        calls.append("yfinance")
        return frame.copy(deep=True)

    def get(url, **kwargs):
        calls.append("stooq" if "stooq" in url else "yahoo_direct")
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload, text="No data")

    monkeypatch.setattr(data_loader.yf, "download", primary)
    monkeypatch.setattr(data_loader.requests, "get", get)
    return calls


def download(*, source="yahoo_direct", clock=OPEN_CLOCK, completed_only=True):
    return data_loader.download_prices("SYNTH", "2021-11-25", "2026-11-24",
        auto_adjust=True, prefer_metadata=source == "yahoo_direct",
        validator=(lambda ticker, frame: validated_completed_daily_prices(
            ticker, frame, now=clock, require_currency=True)) if completed_only else None)


@pytest.mark.parametrize("source", ["yahoo_direct", "yfinance"])
@pytest.mark.parametrize("field,bad", [("open", 105.), ("open", np.nan), ("volume", -1.)])
def test_incoherent_open_session_cannot_reject_verified_previous_close(monkeypatch, source, field, bad):
    raw = bars()
    raw.loc["2026-11-24", field] = bad
    original = raw.copy(deep=True)
    calls = install_sources(monkeypatch, raw)

    result = download(source=source)

    assert result.index.tolist() == [pd.Timestamp("2026-11-23")]
    assert result.close.iloc[-1] == 100.
    assert result.attrs["display_currency"] == "USD"
    assert result.attrs["quote_units_normalized"] is True
    assert result.attrs["hourly_market_schedule"]["excluded_rows"] == 1
    assert result.attrs["provider_attempts"] == []
    assert calls == [source]
    pd.testing.assert_frame_equal(raw, original)


def test_recently_closed_bar_remains_excluded_until_publication_margin(monkeypatch):
    raw = bars()
    raw.loc["2026-11-24", "open"] = 105.
    install_sources(monkeypatch, raw)

    result = download(clock="2026-11-24T21:10:00Z")

    assert result.index[-1] == pd.Timestamp("2026-11-23")


def test_same_invalid_bar_is_rejected_once_its_session_is_completed(monkeypatch):
    raw = bars()
    raw.loc["2026-11-24", "open"] = 105.
    install_sources(monkeypatch, raw)

    with pytest.raises(data_loader.DataDownloadError) as failed:
        download(clock="2026-11-24T21:21:00Z")
    assert failed.value.code == "invalid_ohlc"


def test_incoherent_historical_closed_bar_is_never_removed_or_repaired(monkeypatch):
    raw = bars(("2026-11-19", "2026-11-23", "2026-11-24"))
    raw.loc["2026-11-19", ["open", "high", "low", "close"]] = [95., 101., 99., 100.]
    original = raw.copy(deep=True)
    install_sources(monkeypatch, raw)

    with pytest.raises(data_loader.DataDownloadError) as failed:
        download()
    assert failed.value.code == "invalid_ohlc"
    pd.testing.assert_frame_equal(raw, original)


def test_without_completed_session_validator_open_bar_ohlc_is_still_checked(monkeypatch):
    raw = bars()
    raw.loc["2026-11-24", "open"] = 105.
    install_sources(monkeypatch, raw)

    with pytest.raises(data_loader.DataDownloadError) as failed:
        download(completed_only=False)
    assert failed.value.code == "invalid_ohlc"


def test_callback_does_not_bypass_common_numeric_validation(monkeypatch):
    raw = bars()
    raw.loc["2026-11-23", "open"] = 105.
    install_sources(monkeypatch, raw)

    with pytest.raises(data_loader.DataDownloadError) as failed:
        data_loader.download_prices("SYNTH", "2021-11-25", "2026-11-24",
            prefer_metadata=True, validator=lambda ticker, frame: frame)
    assert failed.value.code == "invalid_ohlc"


def test_discarded_open_bar_cannot_supply_missing_200th_analysis_session(monkeypatch):
    calendar = exchange_calendars.get_calendar("XNYS")
    completed = calendar.sessions_in_range("2025-01-01", "2026-11-23")[-199:]
    raw = bars(completed.append(pd.DatetimeIndex(["2026-11-24"])))
    raw.loc["2026-11-24", "open"] = 105.
    install_sources(monkeypatch, raw)

    result = download()

    assert len(result) == 199
    assert StrategyConfig().sma_long == 200
    prepared = add_signal_columns(add_indicators(result, StrategyConfig()), StrategyConfig())
    with pytest.raises(ValueError, match="suficiente histórico"):
        evaluate_latest_signal(prepared, StrategyConfig(), ticker="SYNTH")
