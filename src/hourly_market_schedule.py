"""Exchange-aware refresh windows and completed *daily* price observations.

This module is only a scheduling/data-availability guard. It fetches nothing,
does not calculate scores and cannot turn an unsupported market into a sell.
Calendars use exchange-calendars (pinned by the application to 4.13.2), including
holidays, exceptional closes, lunch breaks and DST. Calendar coverage is not a
claim that a free provider has already published its final adjusted prices.

``now`` and supplied ``last_success_at`` are timezone-aware instants. A daily
frame index consists of midnight session-date labels, not intraday event
timestamps: their visible dates are preserved, including UTC-midnight labels.
Non-midnight provider timestamps require a verified upstream session-mapping
contract and are rejected here rather than guessing from their UTC date.
"""
from __future__ import annotations

import re

import exchange_calendars as calendars
from exchange_calendars.errors import CalendarError
import pandas as pd


HOURLY_MARKET_SCHEDULE_VERSION = "exchange-hourly-completed-daily-v1"
REFRESH_INTERVAL = pd.Timedelta(hours=1)
FINAL_REFRESH_WINDOW = pd.Timedelta(minutes=90)
DAILY_CLOSE_PUBLICATION_MARGIN = pd.Timedelta(minutes=20)

_SUFFIX_CALENDARS = {
    ".PA": "XPAR", ".L": "XLON", ".IL": "XLON", ".MC": "XMAD",
    ".AS": "XAMS", ".T": "XTKS", ".HK": "XHKG", ".KS": "XKRX",
    ".KQ": "XKRX", ".TW": "XTAI", ".TO": "XTSE", ".V": "XTSX",
    ".MI": "XMIL", ".SW": "XSWX", ".AX": "XASX", ".DE": "XETR",
    ".F": "XFRA", ".CO": "XCSE", ".ST": "XSTO", ".HE": "XHEL",
    ".OL": "XOSL", ".IR": "XDUB", ".VI": "XWBO", ".WA": "XWAR",
    ".LS": "XLIS", ".BR": "XBRU", ".SI": "XSES", ".SS": "XSHG",
    ".SA": "BVMF", ".MX": "XMEX", ".NZ": "XNZE", ".TA": "XTAE",
    ".JO": "XJSE", ".IS": "XIST", ".US": "XNYS",
}
_US_INDICES = frozenset({"^GSPC", "^NDX", "^IXIC", "^DJI", "^RUT", "^VIX"})
_FOREIGN_INDICES = {"^IBEX": "XMAD", "^FTSE": "XLON", "^GDAXI": "XETR",
                    "^FCHI": "XPAR", "^AEX": "XAMS", "^SSMI": "XSWX",
                    "^GSPTSE": "XTSE", "^AXJO": "XASX", "^HSI": "XHKG"}
_US_SHARE_CLASSES = frozenset({"BRK.A", "BRK.B", "BF.A", "BF.B"})
_UNSUPPORTED_PAIR = re.compile(r"-(?:USD|USDT|USDC|BTC|ETH|EUR|GBP|JPY)$")
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_BARE_US_SYMBOL = re.compile(r"^[A-Z][A-Z0-9]{0,9}(?:-[A-Z0-9]{1,2})?$")
_FOREIGN_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9-]*$")


def _calendar_name(ticker: object) -> str | None:
    if not isinstance(ticker, str):
        return None
    symbol = ticker.strip().upper()
    # Yahoo forex (=X), futures (=F), crypto pairs and ISIN identifiers are
    # deliberately not assigned an equities calendar or a US default.
    if (not symbol or "=" in symbol or _ISIN.fullmatch(symbol)
            or _UNSUPPORTED_PAIR.search(symbol)):
        return None
    if symbol in _US_INDICES or symbol in _US_SHARE_CLASSES:
        return "XNYS"
    if symbol in _FOREIGN_INDICES:
        return _FOREIGN_INDICES[symbol]
    if "." in symbol:
        base, suffix = symbol.rsplit(".", 1)
        if not _FOREIGN_SYMBOL.fullmatch(base):
            return None
        return _SUFFIX_CALENDARS.get("." + suffix)
    return "XNYS" if _BARE_US_SYMBOL.fullmatch(symbol) else None


def _calendar(ticker: object):
    name = _calendar_name(ticker)
    if name is None:
        return None
    try:
        return calendars.get_calendar(name)
    except (CalendarError, ValueError, KeyError, TypeError):
        return None


def _utc_clock(value: object) -> pd.Timestamp:
    try:
        clock = pd.Timestamp(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("The reference time must be a timezone-aware instant") from exc
    if pd.isna(clock) or clock.tzinfo is None:
        raise ValueError("The reference time must include a timezone")
    return clock.tz_convert("UTC")


def price_refresh_due(ticker: str, *, now: object,
                      last_success_at: object | None = None) -> bool:
    """Refresh at most hourly while open or for 90 minutes after its close.

    A missing last success permits the first refresh in a valid window. Future,
    malformed or naive success timestamps fail closed rather than guessing a
    timezone. Unsupported markets, holidays and times outside calendar coverage
    never trigger a fetch. Lunch breaks are not market-open windows.
    """
    clock = _utc_clock(now)
    calendar = _calendar(ticker)
    if calendar is None:
        return False
    if last_success_at is not None:
        try:
            previous = _utc_clock(last_success_at)
        except ValueError:
            return False
        elapsed = clock - previous
        if elapsed < REFRESH_INTERVAL:
            return False
    try:
        if calendar.is_open_on_minute(clock.floor("min")):
            return True
        # Use actual close instants rather than UTC/local calendar days. This
        # also handles Sydney sessions whose opening is on the prior UTC date.
        closes = calendar.schedule["close"]
        return bool(((closes <= clock) & (clock <= closes + FINAL_REFRESH_WINDOW)).any())
    except (CalendarError, ValueError, KeyError, TypeError, OverflowError):
        return False


def completed_daily_prices(ticker: str, frame: pd.DataFrame, *,
                           now: object) -> pd.DataFrame:
    """Copy only official sessions with close + 20 minutes already observed.

    No forward-fill, resampling, price changes or signal calculations occur.
    Partial current-session bars, future labels, holidays, invalid labels and
    unverified non-midnight timestamps are removed. An unknown exchange returns
    an empty frame with an explicit
    ``hourly_market_schedule`` diagnostic; callers must skip that analysis,
    not translate missing observations into a trading signal.
    """
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("Daily prices must be a pandas DataFrame")
    clock = _utc_clock(now)
    calendar = _calendar(ticker)
    diagnosis = {
        "version": HOURLY_MARKET_SCHEDULE_VERSION,
        "calendar": _calendar_name(ticker),
        "as_of": clock.isoformat(),
        "publication_margin_minutes": 20,
        "index_contract": "midnight_session_dates_only",
        "not_a_sell_signal": True,
    }
    if calendar is None:
        result = frame.iloc[:0].copy(deep=True)
        diagnosis.update(status="unsupported_market", excluded_rows=len(frame))
    else:
        try:
            closes = calendar.schedule["close"]
            completed = calendar.schedule.index[
                closes + DAILY_CLOSE_PUBLICATION_MARGIN <= clock
            ]
            completed_dates = set(completed.date)
            keep = []
            invalid_labels = 0
            unverified_labels = 0
            for value in frame.index:
                # A numeric RangeIndex is not a date-labelled daily series.
                if isinstance(value, (int, float)):
                    label = pd.NaT
                else:
                    try:
                        label = pd.Timestamp(value)
                    except (ValueError, TypeError, OverflowError):
                        label = pd.NaT
                valid = not pd.isna(label)
                invalid_labels += not valid
                # A Yahoo-chart daily epoch may be the opening instant rather
                # than a date label (Sydney 23:00 UTC belongs to the NEXT local
                # session). Even when the visible UTC day happens to match US
                # trading, no provider contract here proves the session label.
                midnight_label = valid and not any((
                    label.hour, label.minute, label.second,
                    label.microsecond, label.nanosecond,
                ))
                unverified_labels += bool(valid and not midnight_label)
                keep.append(midnight_label and label.date() in completed_dates)
            result = frame.loc[keep].copy(deep=True)
            diagnosis.update(
                status=("unverified_daily_session_labels" if unverified_labels
                        else "completed_sessions_only"),
                latest_completed_session=(completed[-1].date().isoformat() if len(completed) else None),
                excluded_rows=len(frame) - len(result),
                invalid_date_labels=invalid_labels,
                unverified_session_labels=unverified_labels,
                requires_provider_session_normalization=bool(unverified_labels),
                calendar_first_session=calendar.first_session.date().isoformat(),
                calendar_last_session=calendar.last_session.date().isoformat(),
            )
        except (CalendarError, ValueError, KeyError, TypeError, OverflowError):
            result = frame.iloc[:0].copy(deep=True)
            diagnosis.update(status="calendar_unavailable", excluded_rows=len(frame))
    result.attrs["hourly_market_schedule"] = diagnosis
    return result
