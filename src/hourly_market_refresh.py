"""Background transport refresh, independent of signals, mail and real trades.

Only the dedicated mutable cache is written. A failed provider preserves the
last successful observation. Hourly quotes are indicative/delayed daily-bar
closes; their retrieval clock is not misrepresented as the exchange quote time.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from io import StringIO
import logging
import math

import pandas as pd

from src.data_loader import (FUNDAMENTAL_SNAPSHOT_VERSION, download_prices,
                             download_fundamental_snapshot)
from src.data_sources import download_ecb_fx_snapshot, benchmark_for_ticker, sector_benchmark
from src.hourly_market_schedule import price_refresh_due, completed_daily_prices, _calendar
from src.instruments import resolve_analysis_ticker
from src.journal import calculate_open_positions
from src.market_cache import MarketCacheStore, encode_prices
from src.portfolio_snapshot import latest_portfolio_snapshot
from src.price_units import normalize_price_frame_units


@dataclass
class RefreshResult:
    symbols: int = 0
    prices_saved: int = 0
    fundamentals_saved: int = 0
    fx_saved: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _json_value(value):
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item"):
        return _json_value(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError("Unsupported provider payload")


def _quiet_call(function, *args, **kwargs):
    # Public CI must not reveal private watchlists through provider log lines.
    previous = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return function(*args, **kwargs)
    finally:
        logging.disable(previous)


def market_universe(journal, *, limit=200):
    """Held instruments first, then favourites; never enumerate other owners."""
    symbols = []
    snapshots = journal.list_portfolio_snapshot_positions()
    if not snapshots.empty:
        current, _ = latest_portfolio_snapshot(snapshots)
        symbols.extend(current.get("analysis_ticker", []))
    operations = journal.list_operations()
    if not operations.empty:
        symbols.extend(calculate_open_positions(operations).get("ticker", []))
    favorites = journal.list_favorites()
    symbols.extend(favorites.get("ticker", []))
    result = []
    for value in symbols:
        if pd.isna(value) or not str(value).strip():
            continue
        ticker = resolve_analysis_ticker(str(value).strip().upper())
        if ticker not in result:
            result.append(ticker)
    if len(result) > limit:
        raise ValueError("Market universe exceeds the configured safe limit")
    return result


def _daily_due(row, now):
    if row is None:
        return True
    stamp = pd.Timestamp(row["observed_at"])
    return stamp.tzinfo is not None and pd.Timedelta(0) <= now - stamp >= pd.Timedelta(hours=24)


def refresh_market_cache(journal, *, now=None, bootstrap=False, max_symbols=200,
                         fundamental_limit=20, price_loader=None,
                         fundamental_loader=None, fx_loader=None, clock=None):
    """Refresh a bounded universe, never generating/persisting a signal.

    Bootstrap explicitly permits one initial collection outside market hours.
    Scheduled runs remain gated by exchange calendars and one-hour success TTL.
    Five years plus a small buffer preserves the UI's default history setting.
    Fundamentals are rotated in bounded daily batches, not fetched hourly.
    """
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if now.tzinfo is None or pd.isna(now):
        raise ValueError("Refresh requires an aware clock")
    now = now.tz_convert("UTC")
    if not 1 <= max_symbols <= 200 or not 0 <= fundamental_limit <= 25:
        raise ValueError("Invalid collection limits")
    clock = clock or (lambda: pd.Timestamp.now(tz="UTC"))
    price_loader = price_loader or download_prices
    fundamental_loader = fundamental_loader or download_fundamental_snapshot
    fx_loader = fx_loader or download_ecb_fx_snapshot
    store = MarketCacheStore(journal)
    targets = market_universe(journal, limit=max_symbols)
    result = RefreshResult(symbols=len(targets))
    heads = {(row["kind"], row["symbol"]): row for row in store.heads()}
    infos = {}

    # Missing/oldest fundamentals first prevents the same first 20 favourites
    # monopolising a daily batch. Closed markets wait unless explicitly seeded.
    eligible = [ticker for ticker in targets if bootstrap or price_refresh_due(ticker, now=now)]
    eligible.sort(key=lambda ticker: heads.get(("fundamentals", ticker), {}).get("observed_at", ""))
    attempted_fundamentals = 0
    for ticker in eligible:
        head = heads.get(("fundamentals", ticker))
        if not _daily_due(head, now) or attempted_fundamentals >= fundamental_limit:
            continue
        attempted_fundamentals += 1
        try:
            info = _quiet_call(fundamental_loader, ticker)
            if not isinstance(info, dict) or info.get("_fundamental_error"):
                raise ValueError("Provider fundamentals unavailable")
            info = _json_value(info)
            if store.put("fundamentals", ticker, info,
                    {"source_version": FUNDAMENTAL_SNAPSHOT_VERSION,
                     "currency": info.get("currency"), "sector": info.get("sector")}, observed_at=clock()):
                infos[ticker] = info
                result.fundamentals_saved += 1
        except Exception as exc:
            result.errors.append("fundamentals:" + type(exc).__name__)
    # Stored sector/currency context is reused; a metadata read does not fetch
    # every full historical series from the database.
    references = {"SPY"}
    for ticker in targets:
        if ticker not in infos:
            infos[ticker] = dict(heads.get(("fundamentals", ticker), {}).get("metadata", {}))
        references.add(benchmark_for_ticker(ticker))
        sector = sector_benchmark(infos[ticker].get("sector"), ticker)
        if sector:
            references.add(sector)
    symbols = list(dict.fromkeys([*targets, *sorted(references)]))
    start = now.date() - timedelta(days=365 * 5 + 30)
    for ticker in symbols:
        head = heads.get(("prices", ticker))
        if not bootstrap and not price_refresh_due(ticker, now=now,
                last_success_at=head["observed_at"] if head else None):
            result.skipped += 1
            continue
        try:
            frame = _quiet_call(price_loader, ticker, start, now.date(), auto_adjust=True)
            calendar = _calendar(ticker)
            if calendar is None or frame.empty:
                raise ValueError("Unverified market/empty history")
            # A session label can be tomorrow in UTC (Australia), but its
            # official opening must already be in the past. Never save a
            # genuinely future bar over the last valid cached observation.
            latest_day = pd.Timestamp(frame.index[-1]).tz_localize(None).normalize()
            if not calendar.is_session(latest_day) or calendar.session_open(latest_day) > now:
                raise ValueError("Future or unverified price session")
            payload = encode_prices(frame)
            currency = infos.get(ticker, {}).get("currency") or frame.attrs.get("quote_currency")
            normalized = normalize_price_frame_units(frame, currency)
            closed = completed_daily_prices(ticker, frame, now=now)
            metadata = {
                "auto_adjust": True, "interval": "1d", "history_start": frame.index[0].isoformat(),
                "history_end": frame.index[-1].isoformat(), "rows": len(frame),
                "quote": {"price": float(normalized.close.iloc[-1]),
                    "currency": normalized.attrs.get("display_currency", ""),
                    "price_date": frame.index[-1].date().isoformat(),
                    "time_precision": "session_date_only", "indicative": True,
                    "source": str(frame.attrs.get("provider") or "Yahoo Finance / fallback")},
                "completed_price_date": closed.index[-1].date().isoformat() if not closed.empty else None,
                "source": str(frame.attrs.get("provider") or "Yahoo Finance / fallback"),
            }
            if store.put("prices", ticker, payload, metadata, observed_at=clock()):
                result.prices_saved += 1
        except Exception as exc:
            result.errors.append("prices:" + type(exc).__name__)
    if bootstrap or eligible:
        fx_head = heads.get(("fx", "ECB"))
        if _daily_due(fx_head, now):
            try:
                fx = _quiet_call(fx_loader)
                if fx.as_of is None or not fx.rates_per_eur or fx.as_of > now.date():
                    raise ValueError("FX observation is undated or future")
                payload = {"as_of": fx.as_of.isoformat(), "rates_per_eur": _json_value(fx.rates_per_eur),
                           "source": fx.source}
                if any(not math.isfinite(float(value)) or float(value) <= 0
                       for value in payload["rates_per_eur"].values()):
                    raise ValueError("Invalid FX observation")
                if store.put("fx", "ECB", payload, {}, observed_at=clock()):
                    result.fx_saved += 1
            except Exception as exc:
                result.errors.append("fx:" + type(exc).__name__)
    return result
