"""Choose owner-scoped, already observed daily inputs without changing signals.

A new short-window provider request can lag the longer hourly transport cache.
Compare complete series, never append prices from different units/providers.
This module reads existing cache rows only; it creates no database or quotes.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from src.hourly_market_schedule import completed_daily_prices
from src.market_cache import MarketCacheStore, decode_prices


def _clock(value=None):
    clock = pd.Timestamp.now(tz="UTC") if value is None else pd.Timestamp(value)
    if pd.isna(clock) or clock.tzinfo is None:
        raise ValueError("An aware input observation clock is required")
    return clock.tz_convert("UTC")


def _window(frame, target_date):
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.hasnans:
        return None
    target = pd.Timestamp(target_date).date()
    start = target - timedelta(days=550)
    result = frame.loc[[start <= stamp.date() <= target for stamp in frame.index]].copy(deep=True)
    return result if not result.empty else None


def _currency(frame):
    # Case is significant: GBP and GBp are not the same price unit.
    if not isinstance(frame, pd.DataFrame):
        return ""
    return str(frame.attrs.get("quote_currency") or frame.attrs.get("price_currency") or "").strip()


def prefer_alert_prices(downloaded, cached_row, *, owner, ticker, target_date, now=None):
    """Return (whole series, selected_cache), preferring a newer closed cache.

    Equality keeps the provider request. An unusable cache does not turn the
    provider result into a failure or a zero score. Dates after the requested
    target and outside the original 550-day warm-up are never introduced.
    """
    original = _window(downloaded, target_date)
    try:
        clock = _clock(now)
        if (not isinstance(cached_row, dict) or cached_row.get("owner") != owner
                or cached_row.get("kind") != "prices" or cached_row.get("symbol") != ticker):
            return original, False
        observed = _clock(cached_row["observed_at"])
        if observed > clock:
            return original, False
        metadata = cached_row.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("auto_adjust") is not True or metadata.get("interval") != "1d":
            return original, False
        declared_closed = pd.Timestamp(metadata.get("completed_price_date"))
        if pd.isna(declared_closed):
            return original, False
        raw = decode_prices(cached_row["payload"])
        if raw.attrs.get("ticker") and str(raw.attrs["ticker"]).upper() != ticker.upper():
            return original, False
        cached = _window(raw, target_date)
        if cached is None:
            return original, False
        # The snapshot must already have been observed after the official
        # close/publication buffer, not merely read after a future close.
        cached = completed_daily_prices(ticker, cached, now=observed)
        cached = cached.loc[[stamp.date() <= declared_closed.date() for stamp in cached.index]].copy(deep=True)
        if cached.empty:
            return original, False
        previous_currency, cached_currency = _currency(original), _currency(raw)
        if previous_currency and cached_currency and previous_currency != cached_currency:
            return original, False
        if original is not None and cached.index[-1].date() <= original.index[-1].date():
            return original, False
        cached.attrs["alert_input_origin"] = "owner_market_cache"
        cached.attrs["alert_input_observed_at"] = observed.isoformat()
        cached.attrs["alert_input_completed_price_date"] = cached.index[-1].date().isoformat()
        return cached, True
    except (ValueError, TypeError, KeyError, OverflowError):
        return original, False


class OwnerAlertMarketInputs:
    """Read-only optional cache bound to one journal, never shared by owners."""

    def __init__(self, journal, symbols, *, target_date: date, now=None):
        self.journal = journal
        self.owner = getattr(journal, "owner", None)
        self.target_date = target_date
        self.now = _clock(now)
        self.rows = {}
        self.requested = set()
        self.store = None
        try:
            self.store = MarketCacheStore(journal)
            self._read(symbols)
        except (ValueError, TypeError, AttributeError):
            # Fake journals, legacy SQLite and an absent transport cache are
            # legitimate. Do not initialise storage to support a fallback.
            self.store = None

    def _read(self, symbols):
        if self.store is None or getattr(self.journal, "owner", None) != self.owner:
            return
        wanted = sorted(set(symbols) - self.requested)
        self.requested.update(wanted)
        try:
            for row in self.store.read_many("prices", wanted):
                self.rows[row["symbol"]] = row
        except (ValueError, TypeError, AttributeError):
            return

    def select(self, ticker, downloaded):
        if getattr(self.journal, "owner", None) != self.owner:
            return _window(downloaded, self.target_date), False
        self._read([ticker])
        return prefer_alert_prices(downloaded, self.rows.get(ticker), owner=self.owner,
            ticker=ticker, target_date=self.target_date, now=self.now)
