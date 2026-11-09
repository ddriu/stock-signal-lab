"""Regression tests for the explicit Radar download path, with no network."""

from __future__ import annotations

import ast
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.data_loader import DataDownloadError
from src.data_sources import ExternalDataError, FxSnapshot


APP = Path(__file__).resolve().parents[1] / "app.py"
OPEN_US = pd.Timestamp("2026-11-09T16:00:00Z")
START = date(2024, 1, 1)
END = OPEN_US.date()


def prices(*dates: str, close: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1000,
        },
        index=pd.DatetimeIndex(dates),
    )


def fake_loader(frames, *, existing=None):
    calls = Counter()
    state = dict(existing or {})

    def download(ticker, *_):
        calls[ticker] += 1
        data = frames[ticker]
        if isinstance(data, Exception):
            raise data
        return data.copy(deep=True)

    progress = SimpleNamespace(progress=lambda *a, **k: None, empty=lambda: None)
    fake_st = SimpleNamespace(session_state=state, progress=lambda *a, **k: progress)
    node = next(
        node
        for node in ast.parse(APP.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef) and node.name == "load_market_data"
    )
    context = {
        "date": date,
        "timedelta": timedelta,
        "pd": pd,
        "st": fake_st,
        "DataDownloadError": DataDownloadError,
        "ExternalDataError": ExternalDataError,
        "FxSnapshot": FxSnapshot,
        "PriceVerification": object,
        "FUNDAMENTAL_SNAPSHOT_VERSION": "test",
        "cached_download": download,
        "cached_fundamentals": lambda *a, **k: {"sector": "Technology"},
        "cached_fx_rates": lambda: FxSnapshot(as_of=None, rates_per_eur={"EUR": 1.0}),
        "benchmark_for_ticker": lambda ticker: "SPY",
        "sector_benchmark": lambda *a: "XLK",
        "growth_fundamental_status": lambda info: "complete",
        "invalidate_view_cache": lambda state: None,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(APP), "exec"), context)
    return context["load_market_data"], state, calls


def test_radar_uses_completed_closes_by_exchange():
    frame = prices("2026-11-06", "2026-11-09")
    load, state, _ = fake_loader({ticker: frame for ticker in ("AAPL", "7203.T", "SPY", "XLK")})
    refreshed = load(
        ["AAPL", "7203.T"], START, END, True,
        review_clock=OPEN_US, review_reference_attempts=set(),
    )

    assert refreshed == {"AAPL", "7203.T"}
    assert state["market_data"]["AAPL"].index[-1].date() == date(2026, 11, 6)
    assert state["market_data"]["7203.T"].index[-1].date() == date(2026, 11, 9)
    assert state["reference_data"]["SPY"].index[-1].date() == date(2026, 11, 6)


def test_radar_does_not_replace_previous_prices_or_count_failures_as_successes():
    previous = prices("2026-11-06")
    load, state, calls = fake_loader(
        {
            "AAPL": prices("2026-10-30"),
            "MSFT": pd.DataFrame(),
            "NVDA": prices("2026-11-06", close=0),
            "AMD": prices("2026-11-09"),
            "TSLA": prices("2026-11-05"),
        },
        existing={"market_data": {"AAPL": previous, "MSFT": previous, "TSLA": previous}},
    )
    refreshed = load(
        ["AAPL", "MSFT", "NVDA", "AMD", "TSLA"], START, END, True,
        merge_existing=True, review_clock=OPEN_US, review_reference_attempts=set(),
    )

    assert not refreshed
    assert state["_last_price_refresh"]["stale"] == ["AAPL", "TSLA"]
    assert state["_last_price_refresh"]["failed"] == ["AAPL", "AMD", "MSFT", "NVDA", "TSLA"]
    assert set(state["market_data"]) == {"AAPL", "MSFT", "TSLA"}
    assert all(frame.equals(previous) for frame in state["market_data"].values())
    assert not calls["SPY"]


def test_previous_close_is_not_updated_after_the_current_session_has_closed():
    frame = prices("2026-11-06")
    load, state, _ = fake_loader({ticker: frame for ticker in ("AAPL", "SPY", "XLK")})
    assert load(
        ["AAPL"], START, END, True, review_clock=OPEN_US, review_reference_attempts=set(),
    ) == {"AAPL"}
    assert load(
        ["AAPL"], START, END, True, merge_existing=True,
        review_clock=pd.Timestamp("2026-11-09T21:21:00Z"), review_reference_attempts=set(),
    ) == set()

    assert state["_last_price_refresh"]["stale"] == ["AAPL"]
    pd.testing.assert_index_equal(state["market_data"]["AAPL"].index, frame.index)


def test_benchmarks_download_at_most_once_per_review_even_when_one_fails():
    frame = prices("2026-11-06")
    load, state, calls = fake_loader(
        {"AAPL": frame, "MSFT": frame, "SPY": frame, "XLK": DataDownloadError("no quote")},
    )
    attempted: set[str] = set()
    for ticker in ("AAPL", "MSFT"):
        assert load(
            [ticker], START, END, True, merge_existing=True, price_refresh_token="same-run",
            review_clock=OPEN_US, review_reference_attempts=attempted,
        ) == {ticker}

    assert attempted == {"SPY", "XLK"}
    assert calls == {"AAPL": 1, "MSFT": 1, "SPY": 1, "XLK": 1}
    assert set(state["reference_data"]) == {"SPY"}
    assert set(state["market_data"]) == {"AAPL", "MSFT"}


@pytest.mark.parametrize("candidate", [
    prices("2026-11-06"), pd.DataFrame(), DataDownloadError("no quote"),
])
def test_review_references_exclude_stale_or_missing_and_do_not_fallback(candidate):
    old = prices("2026-11-06", close=30.0)
    fresh = prices("2026-11-09")
    load, state, _ = fake_loader(
        {"AAPL": fresh, "SPY": candidate, "XLK": fresh},
        existing={"reference_data": {"SPY": old}},
    )
    current: dict[str, pd.DataFrame] = {}
    attempts: set[str] = set()
    assert load(
        ["AAPL"], START, END, True, merge_existing=True,
        review_clock=pd.Timestamp("2026-11-09T21:21:00Z"),
        review_reference_attempts=attempts, review_reference_data=current,
    ) == {"AAPL"}

    assert attempts == {"SPY", "XLK"}
    assert set(current) == {"XLK"}
    pd.testing.assert_frame_equal(state["reference_data"]["SPY"], old)


def test_fresh_asset_supplies_its_reference_without_another_download():
    fresh = prices("2026-11-09")
    load, _, calls = fake_loader({ticker: fresh for ticker in ("AAPL", "SPY", "XLK")})
    current: dict[str, pd.DataFrame] = {}
    attempts: set[str] = set()
    assert load(
        ["SPY", "AAPL"], START, END, True,
        review_clock=pd.Timestamp("2026-11-09T21:21:00Z"),
        review_reference_attempts=attempts, review_reference_data=current,
    ) == {"SPY", "AAPL"}

    assert set(current) == attempts == {"SPY", "XLK"}
    assert calls["SPY"] == 1


def test_forced_radar_fundamentals_record_their_own_observation_time():
    frame = prices("2026-11-06")
    load, state, _ = fake_loader({ticker: frame for ticker in ("AAPL", "MSFT", "SPY", "XLK")})
    for ticker in ("AAPL", "MSFT"):
        load(
            [ticker], START, END, True, merge_existing=True, refresh_fundamentals=True,
            review_clock=OPEN_US, review_reference_attempts=set(),
        )

    assert state["_last_price_refresh"]["succeeded"] == ["MSFT"]
    assert all(
        pd.Timestamp(info["_ui_observed_at_utc"]).tzinfo is not None
        for info in state["fundamental_data"].values()
    )


def test_existing_individual_refresh_still_accepts_today_but_rejects_empty_response():
    frame = prices("2026-11-09")
    load, state, _ = fake_loader(
        {"AAPL": frame, "MSFT": pd.DataFrame(), "SPY": frame, "XLK": frame},
    )
    assert load(["AAPL", "MSFT"], START, END, True) == {"AAPL"}
    assert state["market_data"]["AAPL"].index[-1].date() == END
    assert "MSFT" not in state["market_data"]


def test_review_requires_an_aware_clock_before_downloading():
    load, _, calls = fake_loader({})
    with pytest.raises(ValueError, match="zona horaria"):
        load(["AAPL"], START, END, True, review_clock=OPEN_US.tz_localize(None))
    assert not calls
