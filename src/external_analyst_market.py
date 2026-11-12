"""Explicit market-data collection for the independent analyst experiment.

Nothing is fetched at import/render time. Quotes are unadjusted OHLC, mapped
to real exchange sessions. Split sessions and missing bars stop a price path;
we never turn a split into a profit/stop or silently bridge missing evidence.
ECB daily rates are a reference-rate approximation, NOT tradable broker FX.
For this experiment their availability is conservatively assumed at 18:00
Europe/Brussels on the observation date, never before an equity opening.
API contract: https://data.ecb.europa.eu/help/api/data-examples
"""
from __future__ import annotations

from datetime import timedelta
from io import StringIO
import math
import re
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from src.data_loader import download_prices
from src.hourly_market_schedule import (
    DAILY_CLOSE_PUBLICATION_MARGIN, _calendar, _utc_clock, completed_daily_prices,
)

MARKET_VERSION = "external-daily-market-v1"
ECB_BASE = "https://data-api.ecb.europa.eu/service/data/EXR/"
MAX_HISTORY_DAYS = 3700
MAX_TICKERS = 250
MAX_FX_AGE = pd.Timedelta(days=7)
HTTP_TIMEOUT = (3.05, 15)
MAX_RESPONSE_BYTES = 5_000_000
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def _finite_positive(value):
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (ValueError, TypeError, OverflowError):
        return False


def _public_quote_metadata(ticker, *, start, end):
    """The public Yahoo chart quote metadata also used by yfinance.

    Separate metadata is necessary because yf.download and the existing loader
    can omit currency/actions. Never infer currency from a ticker suffix. All
    network requests have connect/read bounds and no retries in this adapter.
    """
    left = pd.Timestamp(start, tz="UTC")
    right = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=2)
    response = requests.get(
        "https://query1.finance.yahoo.com/v8/finance/chart/" + quote(ticker, safe=""),
        params={"period1": int(left.timestamp()), "period2": int(right.timestamp()),
                "interval": "1d", "events": "splits"},
        headers={"User-Agent": "Stock-Signal-Lab/external-paper-v1"}, timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError("Quote metadata response exceeds the bounded daily window")
    data = response.json()["chart"]["result"][0]
    meta = data.get("meta") or {}
    timezone = ZoneInfo(meta["exchangeTimezoneName"])
    splits = {}
    for split in (data.get("events") or {}).get("splits", {}).values():
        session = pd.Timestamp(split["date"], unit="s", tz="UTC").tz_convert(timezone).date().isoformat()
        ratio = float(split["numerator"]) / float(split["denominator"])
        if not _finite_positive(ratio):
            raise ValueError("Invalid split ratio")
        splits[session] = ratio
    return {"currency": meta.get("currency"), "split_events": splits,
            "corporate_actions_verified": True, "source": "Yahoo chart public quote metadata"}


def download_ecb_history(currency, *, start, end, request_get=None):
    """Return historical daily EUR-per-unit observations, with no current FX.

    Inject request_get for isolated tests. ECB SP00 gives foreign units per EUR;
    this function validates the dimensions and inverts each observation once.
    """
    if not _CURRENCY.fullmatch(str(currency)) or currency == "EUR":
        raise ValueError("A non-EUR ISO quote currency is required")
    get = request_get or requests.get
    response = get(ECB_BASE + f"D.{currency}.EUR.SP00.A",
                   params={"startPeriod": str(start), "endPeriod": str(end), "format": "csvdata"},
                   headers={"Accept": "text/csv"}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError("FX response exceeds the bounded historical window")
    frame = pd.read_csv(StringIO(response.text))
    required = {"TIME_PERIOD", "OBS_VALUE", "CURRENCY", "CURRENCY_DENOM", "FREQ", "EXR_TYPE"}
    if not required.issubset(frame.columns):
        raise ValueError("ECB historical FX dimensions are incomplete")
    if (not frame["CURRENCY"].eq(currency).all() or not frame["CURRENCY_DENOM"].eq("EUR").all()
            or not frame["FREQ"].eq("D").all() or not frame["EXR_TYPE"].eq("SP00").all()):
        raise ValueError("Unexpected ECB historical series")
    records, seen = [], {}
    for row in frame.to_dict("records"):
        day = pd.Timestamp(row["TIME_PERIOD"])
        if pd.isna(day) or day.tzinfo is not None or day != day.normalize():
            raise ValueError("Invalid ECB observation date")
        if not pd.Timestamp(start) <= day <= pd.Timestamp(end):
            continue
        if not _finite_positive(row["OBS_VALUE"]):
            raise ValueError("Invalid ECB observation value")
        value = float(row["OBS_VALUE"])
        if day in seen and seen[day] != value:
            raise ValueError("Conflicting ECB observations on one date")
        if day in seen:
            continue
        seen[day] = value
        available = (day + pd.Timedelta(hours=18)).tz_localize("Europe/Brussels").tz_convert("UTC")
        records.append({"date": day.date().isoformat(), "currency": currency,
                        "fx_eur_per_unit": 1 / value, "fx_observed_at": available.isoformat(),
                        "source": "ECB EXR D.SP00.A", "availability_is_assumption": True})
    return sorted(records, key=lambda row: row["date"])


def _safe_error(error):
    # Provider exceptions can include URLs/headers. Keep a safe, useful class,
    # not arbitrary response text, credentials, full mailbox data or payloads.
    return type(error).__name__


def load_simulation_bars(calls, *, as_of, activation_at, downloader=None,
                         metadata_downloader=None, fx_downloader=None):
    """Collect completed bars only when an explicit paper runner calls this.

    downloader(ticker,start,end,auto_adjust=False) -> DataFrame, with daily
    session labels and OHLC. metadata_downloader(ticker,*,start,end) -> mapping
    {currency, corporate_actions_verified, split_events:{YYYY-MM-DD:ratio}}.
    fx_downloader(currency,*,start,end) -> records from download_ecb_history.
    A custom OHLC frame may carry those three metadata keys in attrs to avoid
    a separate metadata request. All callbacks are injectable; test cases never
    require a provider. Missing sessions are explicit data_issue bar markers.
    """
    clock, activation = _utc_clock(as_of), _utc_clock(activation_at)
    if activation > clock:
        raise ValueError("Paper tracking cannot be activated in the future")
    if clock - activation > pd.Timedelta(days=MAX_HISTORY_DAYS):
        raise ValueError("The explicit daily collection window exceeds ten years")
    records = [dict(call) for call in calls]
    issues, sources, bars, ticker_calls = [], [], [], {}

    def issue(code, ticker=None, session=None, detail=None):
        row = {"code": code, "ticker": ticker, "session": session}
        if detail:
            row["detail"] = detail
        if row not in issues:
            issues.append(row)

    for call in records:
        ticker = str(call.get("ticker") or "").strip().upper()
        try:
            known = max(_utc_clock(call["received_at"]), _utc_clock(call["observed_at"]))
        except (ValueError, TypeError, KeyError):
            issue("call_clock_unverified", ticker)
            continue
        if known <= clock and ticker:
            ticker_calls.setdefault(ticker, []).append(call)
    if len(ticker_calls) > MAX_TICKERS:
        raise ValueError("Explicit collection is limited to 250 distinct tickers per run")
    downloader = downloader or download_prices
    metadata_downloader = metadata_downloader or _public_quote_metadata
    fx_downloader = fx_downloader or download_ecb_history
    fx_cache = {}
    expected_count = 0
    start_day, end_day = activation.date(), clock.date()
    fx_start = (activation - pd.Timedelta(days=10)).date().isoformat()

    for ticker in sorted(ticker_calls):
        calendar = _calendar(ticker)
        if calendar is None:
            issue("unsupported_exchange_calendar", ticker)
            continue
        expected = calendar.schedule
        expected = expected[(expected["open"] > activation)
                            & (expected["close"] + DAILY_CLOSE_PUBLICATION_MARGIN <= clock)]
        if expected.empty:
            issue("no_completed_post_activation_session", ticker)
            continue
        # Never fetch historic OHLC before activation, even for an older mail.
        expected = expected[expected.index.date >= start_day]
        expected_count += len(expected)
        if expected.empty:
            continue
        earliest = expected.index.min().date()
        latest = expected.index.max().date()
        try:
            raw = downloader(ticker, earliest, max(latest, earliest + timedelta(days=1)), auto_adjust=False)
            if not isinstance(raw, pd.DataFrame):
                raise ValueError("Daily OHLC downloader did not return a frame")
            frame = raw.copy()
            if all(name in frame.attrs for name in ("quote_currency", "corporate_actions_verified", "split_events")):
                meta = {"currency": frame.attrs["quote_currency"],
                        "corporate_actions_verified": frame.attrs["corporate_actions_verified"],
                        "split_events": frame.attrs["split_events"], "source": "OHLC provider metadata"}
            else:
                meta = metadata_downloader(ticker, start=earliest, end=latest)
            currency = str(meta.get("currency") or "")
            reported_currency = frame.attrs.get("quote_currency")
            if not _CURRENCY.fullmatch(currency) or reported_currency and reported_currency != currency:
                raise ValueError("Unknown, sub-unit or conflicting quote currency")
            if meta.get("corporate_actions_verified") is not True or not isinstance(meta.get("split_events"), dict):
                raise ValueError("Split coverage was not verified")
            if any(call.get("currency") != currency for call in ticker_calls[ticker]):
                issue("quote_currency_mismatch", ticker)
                continue
            frame.columns = [str(name).strip().lower() for name in frame.columns]
            completed = completed_daily_prices(ticker, frame, now=clock)
            sources.append({"ticker": ticker, "provider": frame.attrs.get("provider", "injected OHLC"),
                            "currency": currency, "metadata_source": meta.get("source"),
                            "calendar": calendar.name, "first_session": str(earliest),
                            "last_session": str(latest), "auto_adjust": False,
                            "source_fetched_at": frame.attrs.get("fetched_at_utc"),
                            "split_coverage_verified": True})
        except Exception as error:
            issue("quote_or_metadata_unavailable", ticker, detail=_safe_error(error))
            continue
        if currency != "EUR" and currency not in fx_cache:
            try:
                fx_cache[currency] = list(fx_downloader(currency, start=fx_start, end=end_day.isoformat()))
            except Exception as error:
                fx_cache[currency] = []
                issue("historical_fx_unavailable", ticker, detail=_safe_error(error))
        # Duplicate same-date bars must not silently win with keep='last'.
        session_rows = {}
        for date, row in completed.iterrows():
            session_rows.setdefault(pd.Timestamp(date).date().isoformat(), []).append(row)
        for session, official in expected.iterrows():
            day = session.date().isoformat()
            opened, closed = official["open"], official["close"]
            item = {"ticker": ticker, "currency": currency, "session": day,
                    "open_at": opened.isoformat(), "close_at": closed.isoformat(),
                    "available_at": (closed + DAILY_CLOSE_PUBLICATION_MARGIN).isoformat(),
                    "price_source": sources[-1]["provider"], "calendar": calendar.name}
            rows = session_rows.get(day, [])
            error_code = None
            if not rows:
                error_code = "provider_missing_session"
            elif len(rows) != 1:
                error_code = "duplicate_session_quotes"
            elif day in meta["split_events"] and meta["split_events"][day] not in {0, 1}:
                error_code = "split_requires_position_adjustment"
            else:
                quote_row = rows[0]
                values = {key: quote_row.get(key) for key in ("open", "high", "low", "close")}
                if (not all(_finite_positive(value) for value in values.values())
                        or float(values["high"]) < max(float(values["open"]), float(values["close"]))
                        or float(values["low"]) > min(float(values["open"]), float(values["close"]))
                        or float(values["low"]) > float(values["high"])):
                    error_code = "invalid_ohlc"
                else:
                    item.update({key: float(value) for key, value in values.items()})
            if currency == "EUR":
                item["fx_eur_per_unit"] = 1.0
            else:
                candidates = []
                for observation in fx_cache[currency]:
                    try:
                        observed = _utc_clock(observation["fx_observed_at"])
                        if (observation["currency"] == currency and observed <= opened
                                and opened - observed <= MAX_FX_AGE
                                and _finite_positive(observation["fx_eur_per_unit"])):
                            candidates.append((observed, observation))
                    except (ValueError, TypeError, KeyError):
                        continue
                if not candidates:
                    error_code = error_code or "historical_fx_not_known_before_open"
                else:
                    observed, rate = max(candidates, key=lambda candidate: candidate[0])
                    same_clock_rates = {float(candidate[1]["fx_eur_per_unit"])
                                        for candidate in candidates if candidate[0] == observed}
                    if len(same_clock_rates) != 1:
                        error_code = error_code or "conflicting_historical_fx_observations"
                    else:
                        item.update(fx_eur_per_unit=float(rate["fx_eur_per_unit"]),
                                    fx_observed_at=observed.isoformat(), fx_source="ECB historical reference",
                                    fx_availability_is_assumption=True)
            if error_code:
                item["data_issue"] = error_code
                issue(error_code, ticker, day)
            bars.append(item)
    return {"version": MARKET_VERSION, "as_of": clock.isoformat(), "activation_at": activation.isoformat(),
            "bars": sorted(bars, key=lambda bar: (bar["open_at"], bar["ticker"])),
            "issues": issues, "sources": sources,
            "coverage": {"requested_tickers": len(ticker_calls), "tickers_with_metadata": len(sources),
                         "expected_completed_sessions": expected_count,
                         "usable_bars": sum(not bar.get("data_issue") for bar in bars),
                         "unverifiable_session_markers": sum(bool(bar.get("data_issue")) for bar in bars),
                         "has_incidents": bool(issues)},
            "limitations": [
                "Explicit paper collection only; no real orders, emails or private-account requests.",
                "FX is a historical ECB reference approximation, not an executable broker rate; 18:00 Brussels is a conservative assumed publication time.",
                "Later provider corrections and current quote-currency metadata are not an immutable market-data vintage.",
                "Daily OHLC has no intrabar sequence; missing/split sessions block inferred later outcomes.",
                "The experiment does not model dividends or perform split position adjustments; split paths are not verifiable.",
            ]}
