"""No providers: real pinned exchange calendars and synthetic daily OHLC."""
import exchange_calendars as calendars
import pandas as pd
import pytest

from src.hourly_market_schedule import (
    completed_daily_prices,
    price_refresh_due,
)


def prices(dates):
    return pd.DataFrame(
        {"open": range(10, 10 + len(dates)), "close": range(11, 11 + len(dates))},
        index=pd.DatetimeIndex(dates, name="session"),
    )


@pytest.mark.parametrize("ticker,calendar", [
    ("EDEN.PA", "XPAR"), ("TRMD.L", "XLON"), ("ITX.MC", "XMAD"),
    ("ASML.AS", "XAMS"), ("7974.T", "XTKS"), ("1801.HK", "XHKG"),
    ("000660.KS", "XKRX"), ("2330.TW", "XTAI"), ("CCO.TO", "XTSE"),
    ("ENI.MI", "XMIL"), ("NESN.SW", "XSWX"), ("ARL.AX", "XASX"),
    ("MA", "XNYS"), ("SPY", "XNYS"), ("QQQ", "XNYS"),
    ("^GSPC", "XNYS"), ("^NDX", "XNYS"), ("BRK.B", "XNYS"),
    ("BRK-B", "XNYS"),
])
def test_supported_markets_use_their_own_open_close_and_completed_session(ticker, calendar):
    official = calendars.get_calendar(calendar)
    row = official.schedule.loc["2026-10-30"]
    assert price_refresh_due(ticker, now=row["open"] + pd.Timedelta(minutes=1))
    assert not price_refresh_due(ticker, now=row["open"] - pd.Timedelta(minutes=1))
    frame = prices(["2026-10-30"])
    assert completed_daily_prices(ticker, frame, now=row["close"] + pd.Timedelta(minutes=19)).empty
    result = completed_daily_prices(ticker, frame, now=row["close"] + pd.Timedelta(minutes=20))
    pd.testing.assert_frame_equal(result, frame)
    assert result.attrs["hourly_market_schedule"]["calendar"] == calendar


def test_us_window_opens_at_exchange_time_and_includes_only_ninety_minutes_after_close():
    assert not price_refresh_due("MA", now="2026-10-30T13:29:59Z")
    assert price_refresh_due("MA", now="2026-10-30T13:30:00Z")
    assert price_refresh_due("MA", now="2026-10-30T20:00:00Z")
    assert price_refresh_due("MA", now="2026-10-30T21:30:00Z")
    assert not price_refresh_due("MA", now="2026-10-30T21:30:01Z")


def test_ttl_is_one_hour_since_success_not_since_calendar_day_or_attempt():
    assert not price_refresh_due("HALO", now="2026-10-30T16:00Z", last_success_at="2026-10-30T15:00:01Z")
    assert price_refresh_due("HALO", now="2026-10-30T16:00Z", last_success_at="2026-10-30T15:00Z")
    assert price_refresh_due("HALO", now="2026-10-30T16:00Z")
    assert not price_refresh_due("HALO", now="2026-10-30T16:00Z", last_success_at="2026-10-30T16:01Z")


def test_holidays_weekends_and_early_closes_follow_official_calendar():
    assert not price_refresh_due("SPY", now="2026-10-31T16:00Z")
    assert not price_refresh_due("SPY", now="2026-11-26T16:00Z")  # Thanksgiving
    assert price_refresh_due("SPY", now="2026-11-27T17:59Z")
    assert price_refresh_due("SPY", now="2026-11-27T19:30Z")  # early 18:00 UTC close
    assert not price_refresh_due("SPY", now="2026-11-27T19:31Z")
    early = prices(["2026-11-26", "2026-11-27"])
    result = completed_daily_prices("SPY", early, now="2026-11-27T18:20Z")
    assert result.index.tolist() == [pd.Timestamp("2026-11-27")]


def test_dst_and_local_clocks_are_converted_as_instants():
    # US DST ends November 1; Europe already switched on October 25.
    assert price_refresh_due("MA", now="2026-10-30T14:30:00+01:00")
    assert not price_refresh_due("MA", now="2026-11-02T14:30:00+01:00")
    assert price_refresh_due("MA", now="2026-11-02T15:30:00+01:00")
    assert not price_refresh_due("EDEN.PA", now="2026-10-30T07:59:00Z")
    assert price_refresh_due("EDEN.PA", now="2026-10-30T08:00:00Z")
    assert price_refresh_due("EDEN.PA", now="2026-10-23T07:00:00Z")
    frame = prices(["2026-11-02"])
    assert completed_daily_prices("MA", frame, now="2026-11-02T20:30:00Z").empty
    assert len(completed_daily_prices("MA", frame, now="2026-11-02T21:20:00Z")) == 1


def test_asian_lunch_breaks_are_not_open_and_do_not_create_post_close_window():
    assert price_refresh_due("1801.HK", now="2026-10-30T03:59Z")
    assert not price_refresh_due("1801.HK", now="2026-10-30T04:30Z")
    assert price_refresh_due("1801.HK", now="2026-10-30T05:01Z")
    assert price_refresh_due("7974.T", now="2026-10-30T02:29Z")
    assert not price_refresh_due("7974.T", now="2026-10-30T02:45Z")
    assert price_refresh_due("7974.T", now="2026-10-30T03:31Z")


def test_sydney_refresh_can_open_on_prior_utc_date():
    assert price_refresh_due("ARL.AX", now="2026-10-29T23:01Z")
    frame = prices(["2026-10-30"])
    assert completed_daily_prices("ARL.AX", frame, now="2026-10-30T05:19Z").empty
    assert len(completed_daily_prices("ARL.AX", frame, now="2026-10-30T05:20Z")) == 1


def test_daily_data_excludes_intraday_current_bar_future_holiday_and_weekend_labels():
    frame = prices(["2026-10-29", "2026-10-30", "2026-10-31", "2026-11-02"])
    current = completed_daily_prices("EXEL", frame, now="2026-10-30T19:00Z")
    assert current.index.tolist() == [pd.Timestamp("2026-10-29")]
    final = completed_daily_prices("EXEL", frame, now="2026-10-30T20:20Z")
    assert final.index.tolist() == [pd.Timestamp("2026-10-29"), pd.Timestamp("2026-10-30")]
    assert final.attrs["hourly_market_schedule"]["excluded_rows"] == 2
    assert final.attrs["hourly_market_schedule"]["not_a_sell_signal"] is True
    assert len(frame) == 4  # no mutation
    final.iloc[0, 0] = -1
    assert frame.iloc[0, 0] == 10


def test_daily_timezone_aware_midnight_is_a_session_label_not_prior_us_day():
    frame = prices(["2026-10-30T00:00:00Z"])
    assert completed_daily_prices("SPY", frame, now="2026-10-30T19:00Z").empty
    final = completed_daily_prices("SPY", frame, now="2026-10-30T20:20Z")
    assert final.index.equals(frame.index)
    local = prices(["2026-10-30T00:00:00-04:00"])
    assert completed_daily_prices("SPY", local, now="2026-10-30T19:00Z").empty


@pytest.mark.parametrize("ticker,opening,now", [
    # The visible UTC date is October 29, while the ASX session is October 30.
    ("ARL.AX", "2026-10-29T23:00:00Z", "2026-10-30T05:30:00Z"),
    # The UTC day coincides in the US, but that coincidence is not a verified
    # provider/session-label contract either.
    ("SPY", "2026-10-30T13:30:00Z", "2026-10-30T20:30:00Z"),
    ("SPY", "2026-10-30T13:30:00", "2026-10-30T20:30:00Z"),
])
def test_daily_epoch_opening_timestamps_are_not_guessed_as_session_dates(ticker, opening, now):
    frame = prices([opening])
    result = completed_daily_prices(ticker, frame, now=now)
    assert result.empty
    diagnosis = result.attrs["hourly_market_schedule"]
    assert diagnosis["status"] == "unverified_daily_session_labels"
    assert diagnosis["unverified_session_labels"] == 1
    assert diagnosis["requires_provider_session_normalization"] is True
    assert diagnosis["not_a_sell_signal"] is True
    assert frame.index.tolist() == [pd.Timestamp(opening)]


def test_mixed_daily_labels_keep_only_verified_midnight_rows_without_relabelling():
    frame = pd.DataFrame(
        {"close": [100, 101]},
        index=pd.DatetimeIndex(["2026-10-29T00:00Z", "2026-10-30T13:30Z"]),
    )
    result = completed_daily_prices("SPY", frame, now="2026-10-30T20:30Z")
    assert result.index.tolist() == [pd.Timestamp("2026-10-29T00:00Z")]
    assert result.attrs["hourly_market_schedule"]["excluded_rows"] == 1
    assert result.attrs["hourly_market_schedule"]["status"] == "unverified_daily_session_labels"


@pytest.mark.parametrize("ticker", [
    "EURUSD=X", "JPY=X", "NG=F", "BTC-USD", "ETH-EUR", "AAPL.UNKNOWN",
    "^UNKNOWN", "US5949181045", "", "2330", "a ticker", None,
])
def test_unsupported_markets_explicitly_fail_closed_without_a_sell(ticker):
    assert not price_refresh_due(ticker, now="2026-10-30T16:00Z")
    result = completed_daily_prices(ticker, prices(["2026-10-29"]), now="2026-10-30T16:00Z")
    assert result.empty
    assert result.attrs["hourly_market_schedule"]["status"] == "unsupported_market"
    assert result.attrs["hourly_market_schedule"]["not_a_sell_signal"] is True


@pytest.mark.parametrize("last", ["2026-10-30T15:00", "not a time", pd.NaT])
def test_malformed_or_naive_success_metadata_never_guesses_utc(last):
    assert not price_refresh_due("SPY", now="2026-10-30T16:00Z", last_success_at=last)


@pytest.mark.parametrize("now", ["2026-10-30T16:00", "not a time", pd.NaT])
def test_reference_clock_requires_an_explicit_timezone(now):
    with pytest.raises(ValueError):
        price_refresh_due("SPY", now=now)
    with pytest.raises(ValueError):
        completed_daily_prices("SPY", prices(["2026-10-29"]), now=now)


def test_invalid_and_numeric_indexes_cannot_become_completed_epoch_sessions():
    invalid = pd.DataFrame({"close": [1, 2, 3]}, index=["bad", "2026-10-29", None])
    result = completed_daily_prices("SPY", invalid, now="2026-10-30T16:00Z")
    assert result.index.tolist() == ["2026-10-29"]
    assert result.attrs["hourly_market_schedule"]["invalid_date_labels"] == 2
    assert completed_daily_prices("SPY", pd.DataFrame({"close": [1, 2]}), now="2026-10-30T16:00Z").empty


def test_empty_frame_preserves_columns_and_existing_metadata():
    frame = prices([])
    frame.attrs["provider"] = "synthetic"
    result = completed_daily_prices("SPY", frame, now="2026-10-30T16:00Z")
    assert result.empty
    assert result.columns.equals(frame.columns)
    assert result.attrs["provider"] == "synthetic"
    assert "hourly_market_schedule" not in frame.attrs
