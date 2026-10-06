"""Conservative freshness checks shared by portfolio, email and virtual lab.

Business-day tolerance is not an exchange holiday calendar. It allows weekends
and short closures, but never turns an undated or old quote into a current one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from math import isfinite

import pandas as pd


@dataclass(frozen=True)
class QuoteFreshness:
    fresh: bool
    status: str
    market_date: date | None
    business_age: int | None


def us_daily_session_is_closed(market_date: object, *, now: object | None = None) -> bool:
    """Conservative time gate for the laboratory's SPY daily-session anchor.

    A weekday bar for today may be recorded only from 16:30 America/New_York.
    This buffers the regular 16:00 core close (and 16:15 options close), as
    published at https://www.nyse.com/trade/hours-calendars. Previous weekdays
    pass; future dates and weekends do not. DST follows the New York timezone,
    never a fixed UTC offset. An injected clock must include its timezone.

    This is not an exchange holiday calendar or proof that a provider has
    finalized its OHLC bar. Early-close days are deliberately delayed until
    16:30, and weekday holiday dates require a genuine dated SPY quote upstream.
    No international instrument's trading hours are inferred from this gate.
    """

    try:
        observed = pd.to_datetime(market_date, errors="coerce")
        if pd.isna(observed):
            return False
        session_date = pd.Timestamp(observed).date()
        clock = pd.Timestamp.now(tz="America/New_York") if now is None else pd.Timestamp(now)
        if pd.isna(clock) or clock.tzinfo is None:
            return False
        clock = clock.tz_convert("America/New_York")
    except (TypeError, ValueError):
        return False
    if session_date.weekday() >= 5 or session_date > clock.date():
        return False
    if session_date < clock.date():
        return True
    return (clock.hour, clock.minute) >= (16, 30)


def quote_freshness(
    as_of: object,
    *,
    reference_date: object | None = None,
    max_business_age: int = 2,
) -> QuoteFreshness:
    """Reject missing, future and stale quote dates without guessing sessions."""

    reference = pd.Timestamp(reference_date if reference_date is not None else date.today())
    parsed = pd.to_datetime(as_of, errors="coerce")
    if pd.isna(parsed):
        return QuoteFreshness(False, "Fecha de precio sin verificar", None, None)
    market_date = pd.Timestamp(parsed).date()
    today = reference.date()
    if market_date > today:
        return QuoteFreshness(False, "Fecha de precio futura", market_date, None)
    # Count weekdays after the quote, not raw calendar days. A Friday close is
    # therefore still usable over the weekend; two extra days tolerate holidays.
    age = len(pd.bdate_range(market_date + timedelta(days=1), today))
    if age > max(0, int(max_business_age)):
        return QuoteFreshness(False, "Precio antiguo: requiere actualización", market_date, age)
    return QuoteFreshness(True, "Precio reciente", market_date, age)


def recent_market_prices(
    frames: dict[str, pd.DataFrame],
    *,
    reference_date: object | None = None,
) -> tuple[dict[str, float], dict[str, pd.Timestamp]]:
    """One dated, finite price map for every current portfolio valuation."""

    prices: dict[str, float] = {}
    dates: dict[str, pd.Timestamp] = {}
    for ticker, frame in frames.items():
        if frame is None or frame.empty or "close" not in frame:
            continue
        observed = frame.index[-1]
        if not quote_freshness(observed, reference_date=reference_date).fresh:
            continue
        try:
            value = float(frame["close"].iloc[-1])
        except (TypeError, ValueError):
            continue
        if isfinite(value) and value > 0:
            prices[ticker] = value
            dates[ticker] = pd.Timestamp(observed)
    return prices, dates
