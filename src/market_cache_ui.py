"""Session hydration from the persistent transport cache, without providers.

Home can read quote headers without downloading any OHLC history. Analysis
loads compressed histories in bounded batches only when its route needs them.
Intraday observations are used for indicative valuation only, not daily signals.
"""
from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
import math
import time

import pandas as pd

from src.data_loader import FUNDAMENTAL_SNAPSHOT_VERSION
from src.data_sources import FxSnapshot, benchmark_for_ticker, sector_benchmark
from src.hourly_market_schedule import completed_daily_prices
from src.market_cache import MarketCacheStore, MarketCacheError, decode_prices
from src.price_units import resolve_quote_unit
from src.view_cache import invalidate_view_cache


MARKET_CACHE_UI_VERSION = "radar-session-provenance-v1"


def _observed_at(value, *, now):
    """Ignore unknown, naive or implausibly future in-session provenance."""
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
        if (pd.isna(stamp) or stamp.tzinfo is None
                or stamp > now + pd.Timedelta(minutes=5)):
            return None
        return stamp
    except (TypeError, ValueError, OverflowError):
        return None


def _price_window(frame, start_day, end_day):
    if start_day is not None:
        frame = frame.loc[frame.index.date >= start_day]
    if end_day is not None:
        frame = frame.loc[frame.index.date <= end_day]
    return frame


_PRICE_UNIT_ATTRS = ("quote_currency", "display_currency", "source_currency",
                     "price_scale", "quote_units_normalized")


def poll_saved_market_revision(journal, state, *, monotonic_now=None):
    """Hourly header-only check for an open UI; initial/warm calls do no I/O."""
    clock = time.monotonic() if monotonic_now is None else float(monotonic_now)
    bucket = state.get("_persistent_market_reads")
    owner = str(journal.owner)
    if state.get("_navigation_view_owner", owner) != owner:
        return False
    if isinstance(bucket, dict) and bucket.get("owner") != owner:
        for key in ("market_data", "fundamental_data", "reference_data", "fx_snapshot",
                    "_cached_market_quotes", "_persistent_market_status", "_last_price_refresh"):
            state.pop(key, None)
        invalidate_view_cache(state)
        bucket = {"owner": owner, "checked": -math.inf, "heads": [], "rows": {}, "fingerprints": {}}
        state["_persistent_market_reads"] = bucket
    if not isinstance(bucket, dict):
        return False
    def revision(heads):
        return sorted((row["kind"], row["symbol"], row["observed_at"]) for row in heads)
    marker = state.get("_persistent_market_poll")
    if not isinstance(marker, dict) or marker.get("owner") != owner:
        state["_persistent_market_poll"] = {"owner": owner, "checked": clock,
                                           "revision": revision(bucket.get("heads", []))}
        return False
    if clock - marker["checked"] < 3600:
        return False
    marker["checked"] = clock
    try:
        heads = MarketCacheStore(journal).heads()
        current = revision(heads)
        changed = current != marker["revision"]
        marker["revision"] = current
        bucket.update(heads=heads, checked=clock)
        return changed
    except Exception:
        return False


def hydrate_saved_market(journal, state, *, tickers, history=False, now=None, auto_adjust=True,
                         start=None, end=None):
    """Read-only; cache failure keeps the current session and marks coverage.

    Returns counts/status, never fabricating scores or downloading on a miss.
    Manual refreshes newer than the saved observation keep priority.
    """
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        raise ValueError("Hydration requires an aware reference clock")
    owner = str(journal.owner)
    bucket = state.get("_persistent_market_reads")
    if isinstance(bucket, dict) and bucket.get("owner") != owner:
        for key in ("market_data", "fundamental_data", "reference_data", "fx_snapshot",
                    "_cached_market_quotes", "_persistent_market_status", "_last_price_refresh"):
            state.pop(key, None)
        state["_market_view_revision"] = int(state.get("_market_view_revision", 0)) + 1
        invalidate_view_cache(state)
    if not isinstance(bucket, dict) or bucket.get("owner") != owner:
        bucket = {"owner": owner, "checked": -math.inf, "heads": [], "rows": {}, "fingerprints": {}}
        state["_persistent_market_reads"] = bucket
    try:
        store = MarketCacheStore(journal)
        if time.monotonic() - bucket["checked"] >= 60:
            heads = store.heads()
            if any(pd.Timestamp(row["observed_at"]) > now + pd.Timedelta(minutes=5) for row in heads):
                raise MarketCacheError("Future cache observation")
            bucket.update(heads=heads, checked=time.monotonic())
        heads = {(row["kind"], row["symbol"]): row for row in bucket["heads"]}
        targets = list(dict.fromkeys(tickers))[:200]
        quote_rows = {ticker: heads["prices", ticker] for ticker in targets if ("prices", ticker) in heads}
        state["_cached_market_quotes"] = quote_rows
        desired = {("fx", "ECB")}
        if history and auto_adjust:
            desired.update(("prices", ticker) for ticker in targets)
            desired.update(("fundamentals", ticker) for ticker in targets)
        pending = [key for key in desired if key in heads and (
            key not in bucket["rows"] or bucket["rows"][key]["observed_at"] != heads[key]["observed_at"])]
        for kind in ("fx", "fundamentals", "prices"):
            symbols = sorted(key[1] for key in pending if key[0] == kind)
            if symbols:
                for row in store.read_many(kind, symbols):
                    if pd.Timestamp(row["observed_at"]) > now + pd.Timedelta(minutes=5):
                        raise MarketCacheError("Future cache payload")
                    bucket["rows"][kind, row["symbol"]] = row
        fx = bucket["rows"].get(("fx", "ECB"))
        if fx:
            payload = fx["payload"]
            as_of = date.fromisoformat(payload["as_of"])
            rates = {currency: float(value) for currency, value in payload["rates_per_eur"].items()}
            if as_of <= now.date() and rates.get("EUR") == 1 and all(
                    math.isfinite(float(value)) and float(value) > 0 for value in rates.values()):
                state["fx_snapshot"] = FxSnapshot(as_of, dict(rates), payload.get("source", "BCE"))
        changed = False
        fingerprints = dict(bucket["fingerprints"])
        if history and auto_adjust:
            start_day = pd.Timestamp(start).date() if start is not None else None
            end_day = pd.Timestamp(end).date() if end is not None else None
            fundamentals = dict(state.get("fundamental_data", {}))
            prices = dict(state.get("market_data", {}))
            references = dict(state.get("reference_data", {}))
            reference_symbols = {"SPY"}
            for ticker in targets:
                row = bucket["rows"].get(("fundamentals", ticker))
                previous_observation = _observed_at(
                    fundamentals.get(ticker, {}).get("_ui_observed_at_utc"), now=now
                )
                if (row and row["metadata"].get("source_version") == FUNDAMENTAL_SNAPSHOT_VERSION
                        and row["payload"].get("_snapshot_version", FUNDAMENTAL_SNAPSHOT_VERSION) == FUNDAMENTAL_SNAPSHOT_VERSION
                        and (previous_observation is None or pd.Timestamp(row["observed_at"]) >= previous_observation)):
                    fingerprint = sha256(json.dumps(row["payload"], sort_keys=True).encode()).hexdigest()
                    if (fingerprints.get(("fundamentals", ticker)) != fingerprint
                            or ticker not in fundamentals or previous_observation is not None):
                        fundamentals[ticker] = row["payload"]
                        fingerprints["fundamentals", ticker] = fingerprint
                        changed = True
                reference_symbols.add(benchmark_for_ticker(ticker))
                sector = sector_benchmark(fundamentals.get(ticker, {}).get("sector"), ticker)
                if sector:
                    reference_symbols.add(sector)
            missing_refs = [ticker for ticker in reference_symbols if ("prices", ticker) in heads and (
                ("prices", ticker) not in bucket["rows"] or bucket["rows"]["prices", ticker]["observed_at"] != heads["prices", ticker]["observed_at"])]
            if missing_refs:
                for row in store.read_many("prices", sorted(missing_refs)):
                    if pd.Timestamp(row["observed_at"]) > now + pd.Timedelta(minutes=5):
                        raise MarketCacheError("Future reference payload")
                    bucket["rows"]["prices", row["symbol"]] = row
            for ticker in sorted(set(targets) | reference_symbols):
                row = bucket["rows"].get(("prices", ticker))
                if not row or row["metadata"].get("auto_adjust") is not True:
                    continue
                row_observed = pd.Timestamp(row["observed_at"])
                # A saved intraday candle does not become a final close
                # merely because the UI reads it after the session ends.
                frame = completed_daily_prices(
                    ticker, decode_prices(row["payload"]), now=min(now, row_observed)
                )
                if "completed_price_date" in row["metadata"]:
                    try:
                        declared_close = date.fromisoformat(row["metadata"]["completed_price_date"])
                    except (TypeError, ValueError):
                        frame = frame.iloc[:0]
                    else:
                        frame = _price_window(frame, None, declared_close)
                frame = _price_window(frame, start_day, end_day)
                destination = prices if ticker in targets else references
                previous = destination.get(ticker)
                if (isinstance(previous, pd.DataFrame) and not previous.empty
                        and isinstance(previous.index, pd.DatetimeIndex)):
                    # Per-frame retrieval time survives all later manual
                    # batches. The last-batch marker is a legacy fallback.
                    manual_observed = _observed_at(previous.attrs.get("fetched_at_utc"), now=now)
                    if manual_observed is None:
                        manual = state.get("_last_price_refresh", {})
                        if ticker in manual.get("succeeded", ()):
                            manual_observed = _observed_at(manual.get("requested_at_utc"), now=now)
                    older_observation = (manual_observed is not None and row_observed < manual_observed)
                    previous_window = _price_window(previous, start_day, end_day)
                    if not previous_window.empty and (older_observation or frame.empty
                            or previous_window.index[-1].date() > frame.index[-1].date()):
                        verified = completed_daily_prices(ticker, previous_window, now=now)
                        if not verified.empty and (older_observation or frame.empty
                                or verified.index[-1].date() > frame.index[-1].date()):
                            # Respect a newly selected date window rather than
                            # keeping rows outside it solely for provenance.
                            if not previous.index.equals(verified.index):
                                destination[ticker] = verified
                                fingerprints.pop(("prices", ticker), None)
                                changed = True
                            continue
                if frame.empty:
                    if ticker in destination:
                        del destination[ticker]
                        fingerprints.pop(("prices", ticker), None)
                        changed = True
                    continue
                units = {key: frame.attrs.get(key) for key in _PRICE_UNIT_ATTRS}
                fingerprint = sha256((frame.to_json(date_format="iso") + json.dumps(units, sort_keys=True)).encode()).hexdigest()
                if (fingerprints.get(("prices", ticker)) != fingerprint
                        or not isinstance(previous, pd.DataFrame) or not previous.equals(frame)
                        or any(previous.attrs.get(key) != value for key, value in units.items())):
                    destination[ticker] = frame
                    fingerprints["prices", ticker] = fingerprint
                    changed = True
            if changed:
                state.update(market_data=prices, fundamental_data=fundamentals, reference_data=references)
                state["_market_view_revision"] = int(state.get("_market_view_revision", 0)) + 1
                invalidate_view_cache(state)
            bucket["fingerprints"] = fingerprints
        # Retain only the latest single revision per instrument, bounded by the
        # transport schema's owner limit. No multi-hour history in UI memory.
        result = {"status": "saved" if quote_rows else "empty", "quotes": len(quote_rows),
                  "requested": len(targets), "histories_loaded": bool(history and auto_adjust),
                  "latest_retrieval": max((r["observed_at"] for r in quote_rows.values()), default=None)}
        state["_persistent_market_status"] = result
        return result
    except Exception as exc:
        result = {"status": "unavailable", "error_type": type(exc).__name__, "quotes": 0,
                  "requested": len(tickers)}
        state["_persistent_market_status"] = result
        return result


def valuation_frames(frames, quotes, *, now=None):
    """Build valuation-only copies; never return this map to a signal engine."""
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    result = dict(frames)
    for ticker, row in quotes.items():
        try:
            quote = row["metadata"]["quote"]
            observed = pd.Timestamp(row["observed_at"])
            day = pd.Timestamp(quote["price_date"])
            price = float(quote["price"])
            currency = str(quote.get("currency") or "")
            unit = resolve_quote_unit(currency)
            price *= unit.price_scale
            currency = unit.display_currency
            if (not currency or len(currency) != 3 or not math.isfinite(price) or price <= 0
                    or observed.tzinfo is None or observed > now + pd.Timedelta(minutes=5)
                    or day.date() > now.date()):
                continue
            previous = result.get(ticker)
            if previous is not None and not previous.empty:
                retrieved = previous.attrs.get("fetched_at_utc")
                if retrieved and pd.Timestamp(retrieved).tzinfo is not None and pd.Timestamp(retrieved) > observed:
                    continue
                existing_currency = str(previous.attrs.get("display_currency") or "")
                if existing_currency and existing_currency != currency:
                    continue
                if previous.index[-1].date() > day.date():
                    continue
            frame = pd.DataFrame({"close": [price]}, index=pd.DatetimeIndex([day]))
            frame.attrs.update(display_currency=currency, quote_currency=currency,
                quote_units_normalized=True, indicative_valuation_only=True)
            result[ticker] = frame
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    return result
