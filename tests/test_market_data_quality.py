import pandas as pd
import pytest

from src.market_data_quality import quote_freshness, recent_market_prices, us_daily_session_is_closed


def test_weekend_accepts_friday_close():
    result = quote_freshness("2026-10-02", reference_date="2026-10-04")
    assert result.fresh and result.business_age == 0


def test_old_missing_and_future_quotes_cannot_be_current():
    for value in ("2020-01-01", None, "bad date", "2026-10-05"):
        assert not quote_freshness(value, reference_date="2026-10-03").fresh


def test_tolerance_is_counted_in_business_days():
    assert quote_freshness("2026-09-30", reference_date="2026-10-03").fresh
    assert not quote_freshness("2026-09-29", reference_date="2026-10-03").fresh


def test_timezone_is_preserved_as_market_date():
    result = quote_freshness("2026-10-02T23:00:00-04:00", reference_date="2026-10-03")
    assert result.fresh and result.market_date.isoformat() == "2026-10-02"


def test_current_portfolio_map_excludes_old_or_invalid_prices():
    frames = {
        "FRESH": pd.DataFrame({"close": [100.0]}, index=pd.to_datetime(["2026-10-02"])),
        "OLD": pd.DataFrame({"close": [90.0]}, index=pd.to_datetime(["2020-01-01"])),
        "INVALID": pd.DataFrame({"close": [float("inf")]}, index=pd.to_datetime(["2026-10-02"])),
    }
    prices, dates = recent_market_prices(frames, reference_date="2026-10-03")
    assert prices == {"FRESH": 100.0}
    assert list(dates) == ["FRESH"]


@pytest.mark.parametrize(
    "market_date,before,closed",
    [
        ("2026-01-15", "2026-01-15T21:29:59Z", "2026-01-15T21:30:00Z"),
        ("2026-07-15", "2026-07-15T20:29:59Z", "2026-07-15T20:30:00Z"),
        ("2026-03-09", "2026-03-09T20:29:59Z", "2026-03-09T20:30:00Z"),
        ("2026-11-02", "2026-11-02T21:29:59Z", "2026-11-02T21:30:00Z"),
        # Europe and the US change their clocks on different weekends.
        ("2026-10-29", "2026-10-29T21:29:59+01:00", "2026-10-29T21:30:00+01:00"),
    ],
)
def test_daily_session_guard_uses_new_york_close_and_dst(market_date, before, closed):
    assert not us_daily_session_is_closed(market_date, now=before)
    assert us_daily_session_is_closed(market_date, now=closed)


@pytest.mark.parametrize("market_date", ["2026-10-03", "2026-10-04", "2026-10-06", None, "bad-date"])
def test_daily_session_guard_rejects_weekend_future_and_missing_dates(market_date):
    assert not us_daily_session_is_closed(market_date, now="2026-10-05T22:00:00Z")


def test_daily_session_guard_compares_dates_in_new_york_not_utc():
    assert not us_daily_session_is_closed("2026-10-06", now="2026-10-06T02:00:00Z")
    assert us_daily_session_is_closed("2026-10-05", now="2026-10-06T02:00:00Z")
    assert us_daily_session_is_closed("2026-10-02", now="2026-10-04T12:00:00Z")


def test_daily_session_guard_requires_a_valid_aware_injected_clock():
    for clock in ("bad-clock", "2026-10-05T22:00:00", pd.NaT):
        assert not us_daily_session_is_closed("2026-10-05", now=clock)
