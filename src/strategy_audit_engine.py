"""Pure, causal, lot-based audit of recorded strong-entry observations.

No signals are generated, no capital is recycled and no account/order/email is
modified. Native-price stops never use FX changes as a price trigger. Exact
intraday TWR is deliberately unavailable; the separately named daily proxy uses
beginning-of-day contributions and retains withdrawals as segregated zero-yield
cash in investor wealth.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import hashlib
import json
import math

import pandas as pd

from config import StrategyConfig


ENGINE_VERSION = "recorded-signals-lots-v1"
ISSUE_COLUMNS = ["code", "ticker", "event_id", "session", "detail", "severity"]
LOT_COLUMNS = ["lot_id", "event_id", "ticker", "signal_session", "observed_at", "entry_at",
               "entry_session", "entry_price_native", "entry_fx", "quantity", "contribution_usd",
               "entry_score", "sector", "status", "exit_at", "exit_price_native", "exit_fx",
               "exit_reason", "proceeds_usd", "realized_pnl_usd", "value_usd", "unrealized_pnl_usd",
               "duration_days", "data_complete", "valuation_at", "valuation_session", "valuation_carried_closed_market"]
TRADE_COLUMNS = ["trade_id", "lot_id", "event_id", "ticker", "entry_at", "exit_at", "decision_at",
                 "entry_price_native", "exit_price_native", "quantity", "entry_fx", "exit_fx",
                 "contribution_usd", "proceeds_usd", "pnl_usd", "return_pct", "exit_reason",
                 "duration_days", "fill_timing", "fx_proxy", "fees_usd", "tax_usd"]
DAILY_COLUMNS = ["date", "contribution_usd", "withdrawal_usd", "contributions_usd", "withdrawals_usd",
                 "open_value_usd", "open_value_known_usd", "value_usd", "value_known_usd",
                 "realized_pnl_usd", "unrealized_pnl_usd", "valuation_complete", "open_lots",
                 "unit_value_daily_approx", "drawdown_daily_approx_pct"]


@dataclass(frozen=True)
class AuditConfig:
    start: object | None = None
    end: object | None = None
    entry_mode: str = "daily"
    min_score: float | None = None
    amount_usd: float = 1.0
    sizing: str = "fixed"
    exit_policy: str = "reduce"
    strategy_config: StrategyConfig = field(default_factory=StrategyConfig)

    def validate(self) -> None:
        if self.entry_mode not in {"daily", "activation"}:
            raise ValueError("entry_mode must be daily or activation")
        if self.sizing not in {"fixed", "tiered", "factors"}:
            raise ValueError("sizing must be fixed, tiered or factors")
        if self.exit_policy not in {"reduce", "sell", "confirmed", "trailing10", "fixed8", "hold", "original"}:
            raise ValueError("Unknown exit policy")
        if _number(self.amount_usd) is None or self.amount_usd <= 0:
            raise ValueError("amount_usd must be finite and positive")
        if self.min_score is not None and (_number(self.min_score) is None or not 0 <= self.min_score <= 100):
            raise ValueError("min_score must be between 0 and 100")
        self.strategy_config.validate()
        start, end = _boundary(self.start, False), _boundary(self.end, True)
        if start is not None and end is not None and start > end:
            raise ValueError("start must not follow end")


@dataclass(frozen=True)
class AuditResult:
    daily: pd.DataFrame
    lots: pd.DataFrame
    trades: pd.DataFrame
    issues: pd.DataFrame
    metrics: dict[str, object]
    manifest: dict[str, object]


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _text(value: object) -> str:
    return "" if value is None or pd.isna(value) else str(value).strip()


def _positive(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _aware(value: object) -> pd.Timestamp | None:
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    return stamp.tz_convert("UTC") if pd.notna(stamp) and stamp.tzinfo is not None else None


def _session(value: object) -> str | None:
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    return stamp.date().isoformat() if pd.notna(stamp) else None


def _boundary(value: object, end: bool) -> pd.Timestamp | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime) or isinstance(value, str) and len(value) == 10:
        day = pd.Timestamp(value).date().isoformat()
        return pd.Timestamp(day + ("T23:59:59.999999999Z" if end else "T00:00:00Z"))
    stamp = _aware(value)
    if stamp is None:
        raise ValueError("Range timestamps must carry a timezone (or be ISO dates)")
    return stamp


def _safe(value: object) -> object:
    if isinstance(value, pd.DataFrame):
        return {"index": _safe(list(value.index)), "records": _safe(value.to_dict("records"))}
    if isinstance(value, pd.Series):
        return _safe(value.tolist())
    if isinstance(value, Mapping):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if value is None or value is pd.NA:
        return None
    if hasattr(value, "item"):
        return _safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(_safe(value), sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _flag(value: object) -> bool | None:
    if value is None or pd.isna(value):
        return None
    if value in (True, 1):
        return True
    if value in (False, 0):
        return False
    return None


def run_strategy_audit(signals: pd.DataFrame, bars: Mapping[str, pd.DataFrame], config: AuditConfig) -> AuditResult:
    """Audit supplied observations, with strict availability and independent lots.

    Range limits select observation times, not backdated signal sessions. Earlier
    observations may establish activation state but never create pre-start buys.
    A missing intended opening is not replaced by a convenient later quote.
    """
    config.validate()
    start, end = _boundary(config.start, False), _boundary(config.end, True)
    issues: list[dict[str, object]] = []

    def issue(code: str, ticker: str = "", event_id: str = "", session: str = "", detail: str = "", severity: str = "warning") -> None:
        issues.append(dict(code=code, ticker=ticker, event_id=event_id, session=session, detail=detail, severity=severity))

    normalized: dict[str, list[dict[str, object]]] = {}
    by_session: dict[str, dict[str, dict[str, object]]] = {}
    quarantines: dict[str, str] = {}
    days: set[str] = set()
    blocked: set[str] = set()
    unavailable_sessions: dict[str, set[str]] = {}
    schedules: dict[str, list[dict[str, object]]] = {}
    scheduled_sessions: dict[str, dict[str, dict[str, object]]] = {}
    expected_session_dates: dict[str, set[str]] = {}
    required_closes: dict[str, pd.Timestamp] = {}
    calendar_ranges: dict[str, tuple[str, str]] = {}
    for raw_ticker, source in sorted(bars.items()):
        ticker = str(raw_ticker).strip().upper()
        if not ticker:
            continue
        frame = source.copy(deep=True)
        quarantine = _session(source.attrs.get("quarantined_from"))
        if quarantine:
            quarantines[ticker] = quarantine
        required_close = _aware(source.attrs.get("required_last_close_at"))
        if required_close is not None:
            required_closes[ticker] = required_close
        rows: list[dict[str, object]] = []
        grouped: dict[str, list[dict[str, object]]] = {}
        for index, row in zip(frame.index, frame.to_dict("records")):
            session = _session(index)
            if session is None:
                issue("invalid_bar_session", ticker)
                continue
            if end is not None and session > end.date().isoformat():
                continue
            days.add(session)
            grouped.setdefault(session, []).append(row)
        for session, candidates in sorted(grouped.items()):
            hashes = {_hash(row) for row in candidates}
            if len(hashes) > 1:
                issue("conflicting_duplicate_bar", ticker, session=session)
                unavailable_sessions.setdefault(ticker, set()).add(session)
                continue
            if len(candidates) > 1:
                issue("duplicate_bar", ticker, session=session, severity="info")
            row = dict(candidates[0])
            opened, closed = _aware(row.get("open_at")), _aware(row.get("close_at"))
            if opened is None or closed is None or closed <= opened:
                issue("invalid_bar_availability", ticker, session=session)
                unavailable_sessions.setdefault(ticker, set()).add(session)
                continue
            if end is not None and opened > end:
                continue
            if quarantine and session >= quarantine:
                issue("quarantined_bar", ticker, session=session)
                continue
            available = _aware(row.get("close_available_at")) if "close_available_at" in row else closed
            if available is None or available < closed:
                issue("invalid_close_knowledge_clock", ticker, session=session)
                # The opening can still be checked, but this close cannot create
                # a decision or valuation. Do not invent a source-arrival time.
                available = None
            row.update(session=session, open_at=opened, close_at=closed, close_available_at=available)
            for field_name in ("open", "high", "low", "close", "fx_open", "fx_close", "sma_long"):
                row[field_name] = _positive(row.get(field_name))
            if all(row.get(key) is not None for key in ("open", "high", "low", "close")) and not (
                row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]
            ):
                issue("inconsistent_ohlc", ticker, session=session)
                for key in ("open", "high", "low", "close"):
                    row[key] = None
            rows.append(row)
        rows.sort(key=lambda item: item["open_at"])
        normalized[ticker] = rows
        by_session[ticker] = {row["session"]: row for row in rows}
        raw_schedule = source.attrs.get("session_schedule")
        if isinstance(raw_schedule, list) and raw_schedule:
            calendar = []
            calendar_valid = True
            for item in raw_schedule:
                if not isinstance(item, Mapping):
                    calendar_valid = False
                    break
                session = _session(item.get("session"))
                opened, closed = _aware(item.get("open_at")), _aware(item.get("close_at"))
                if session is None or opened is None or closed is None or closed <= opened:
                    calendar_valid = False
                    break
                calendar.append(dict(session=session, open_at=opened, close_at=closed))
            if calendar_valid and len({item["session"] for item in calendar}) == len(calendar):
                calendar.sort(key=lambda item: item["open_at"])
                schedules[ticker] = calendar
                calendar_ranges[ticker] = (
                    _session(source.attrs.get("calendar_coverage_start")) or calendar[0]["session"],
                    _session(source.attrs.get("calendar_coverage_end")) or calendar[-1]["session"],
                )
                scheduled_sessions[ticker] = {item["session"]: item for item in calendar}
                expected_session_dates.setdefault(ticker, set()).update(scheduled_sessions[ticker])
                for expected in calendar:
                    if end is not None and expected["open_at"] > end:
                        continue
                    days.add(expected["session"])
                    if expected["session"] not in by_session[ticker]:
                        unavailable_sessions.setdefault(ticker, set()).add(expected["session"])
                        issue("missing_expected_bar", ticker, session=expected["session"])
            else:
                issue("invalid_session_schedule", ticker)
        for expected in source.attrs.get("expected_sessions", []) or []:
            session = _session(expected)
            if session is not None:
                expected_session_dates.setdefault(ticker, set()).add(session)
                if (end is None or session <= end.date().isoformat()) and session not in by_session[ticker]:
                    unavailable_sessions.setdefault(ticker, set()).add(session)
                    days.add(session)
        if source.attrs.get("audit_blocked") and not rows:
            blocked.add(ticker)

    raw_records = signals.to_dict("records")
    event_groups: dict[str, list[dict[str, object]]] = {}
    for row in raw_records:
        available = _aware(row.get("observed_at"))
        if end is not None and available is not None and available > end:
            continue
        identifier = _text(row.get("event_id"))
        if not identifier:
            issue("missing_event_id")
            continue
        event_groups.setdefault(identifier, []).append(row)
    observations: list[dict[str, object]] = []
    for identifier, candidates in sorted(event_groups.items()):
        if len({_hash(row) for row in candidates}) > 1:
            issue("conflicting_event_id", event_id=identifier)
            continue
        if len(candidates) > 1:
            issue("duplicate_event_id", event_id=identifier, severity="info")
        row = dict(candidates[0])
        ticker = _text(row.get("ticker")).upper()
        observed, session = _aware(row.get("observed_at")), _session(row.get("signal_session"))
        if not ticker or observed is None or session is None or not _text(row.get("entry_label")):
            issue("invalid_signal_availability", ticker, identifier)
            continue
        if end is not None and observed > end:
            continue
        bar = by_session.get(ticker, {}).get(session)
        if session > observed.date().isoformat():
            issue("signal_before_session_close", ticker, identifier, session)
            continue
        if bar is not None and bar["close_at"] > observed:
            issue("intraday_observation_partial", ticker, identifier, session,
                  "Recorded label was available before this session's final close; preserve it, do not reconstruct the entry from the future close.")
        row.update(event_id=identifier, ticker=ticker, observed_at=observed, signal_session=session)
        observations.append(row)
    observations.sort(key=lambda row: (row["observed_at"], row["event_id"]))
    first: list[dict[str, object]] = []
    seen_sessions: set[tuple[str, str]] = set()
    for row in observations:
        key = (row["ticker"], row["signal_session"])
        if key in seen_sessions:
            issue("later_session_observation_ignored", row["ticker"], row["event_id"], row["signal_session"], severity="info")
            continue
        seen_sessions.add(key)
        first.append(row)

    eligible: list[dict[str, object]] = []
    orders: dict[tuple[str, pd.Timestamp], list[dict[str, object]]] = {}
    previous: dict[str, bool] = {}
    missing_tickers: set[str] = set()
    for row in first:
        ticker, identifier, session = row["ticker"], row["event_id"], row["signal_session"]
        strong = row.get("entry_label") == "Entrada fuerte"
        known_before = ticker in previous
        was_strong = previous.get(ticker, False)
        previous[ticker] = strong
        if not strong or start is not None and row["observed_at"] < start:
            continue
        if config.entry_mode == "activation":
            if not known_before:
                issue("left_censored_activation", ticker, identifier, session)
                continue
            if was_strong:
                continue
        score = _number(row.get("entry_score"))
        if config.min_score is not None and (score is None or not 0 <= score <= 100):
            issue("missing_filter_score", ticker, identifier, session)
            continue
        if config.min_score is not None and (score is None or score < config.min_score):
            continue
        eligible.append(row)
        amount = float(config.amount_usd)
        if config.sizing != "fixed":
            if score is None or not 75 <= score <= 100:
                issue("missing_sizing_score", ticker, identifier, session)
                continue
            amount *= 1 if score < 80 else 2 if score < 85 else 3
            if config.sizing == "factors":
                factors = [_number(row.get(name)) for name in ("company_score", "valuation_score", "opportunity_score", "risk_score")]
                if any(value is None or not 0 <= value <= 100 for value in factors):
                    issue("missing_historical_sizing_factors", ticker, identifier, session)
                    continue
                amount *= min(factors) / 100.0
        if amount <= 0:
            issue("zero_sizing_amount", ticker, identifier, session)
            continue
        if ticker in blocked:
            issue("blocked_asset", ticker, identifier, session)
            continue
        rows = normalized.get(ticker, [])
        intended = next((bar for bar in schedules.get(ticker, []) if bar["open_at"] > row["observed_at"]), None)
        if ticker in schedules:
            if intended is None or end is not None and intended["open_at"] > end:
                issue("no_next_open_in_range", ticker, identifier, session)
                continue
            opening = by_session.get(ticker, {}).get(intended["session"])
            if opening is None:
                issue("missing_intended_open", ticker, identifier, intended["session"])
                continue
            if opening["open_at"] != intended["open_at"]:
                issue("schedule_bar_time_mismatch", ticker, identifier, intended["session"])
                continue
        else:
            opening = next((bar for bar in rows if bar["open_at"] > row["observed_at"]), None)
        if opening is None:
            if not rows:
                missing_tickers.add(ticker)
            code = "quarantined_entry" if ticker in quarantines and session >= quarantines[ticker] else "no_next_open"
            issue(code, ticker, identifier, session)
            continue
        if any(row["observed_at"].date().isoformat() <= missing <= opening["session"]
               for missing in unavailable_sessions.get(ticker, set())):
            issue("unverifiable_next_open", ticker, identifier, session)
            continue
        if end is not None and opening["open_at"] > end:
            issue("no_next_open_in_range", ticker, identifier, session)
            continue
        orders.setdefault((ticker, opening["open_at"]), []).append({**row, "amount_usd": amount})

    if end is None:
        timestamps = [row["close_at"] for rows in normalized.values() for row in rows]
        timestamps += [row["observed_at"] for row in first]
        end = max(timestamps) if timestamps else None
        if days and (end is None or max(days) > end.date().isoformat()):
            end = _boundary(max(days), True)
    if end is not None:
        days.add(end.date().isoformat())
    events = []
    for ticker, rows in normalized.items():
        for row in rows:
            if end is None or row["open_at"] <= end:
                events.append((row["open_at"], 0, ticker, row))
            known_at = row["close_available_at"]
            if known_at is not None and (end is None or known_at <= end):
                events.append((known_at, 1, ticker, row))
    events.sort(key=lambda item: (item[0], item[1], item[2]))
    lots: list[dict[str, object]] = []
    ticker_lots: dict[str, list[dict[str, object]]] = {}
    trades: list[dict[str, object]] = []
    daily_rows: list[dict[str, object]] = []

    def stop_level(lot: dict[str, object]) -> float | None:
        if config.exit_policy == "fixed8":
            return lot["entry_price_native"] * .92
        if config.exit_policy == "trailing10":
            return lot["_peak"] * .90
        if config.exit_policy == "original":
            hard = lot["entry_price_native"] * (1 - config.strategy_config.stop_loss_pct / 100)
            trail = lot["_peak"] * (1 - config.strategy_config.trailing_stop_pct / 100) if config.strategy_config.trailing_stop_pct else 0
            return max(hard, trail)
        return None

    def close_lot(lot: dict[str, object], row: dict[str, object], when: pd.Timestamp, price: float, reason: str, decision: pd.Timestamp, timing: str) -> None:
        fx = row.get("fx_open")
        proxy = timing == "intraday_unknown_before_close"
        proceeds = lot["quantity"] * price * fx if fx is not None else None
        pnl = proceeds - lot["contribution_usd"] if proceeds is not None else None
        if fx is None:
            issue("missing_exit_fx", lot["ticker"], lot["event_id"], row["session"])
        if proxy:
            issue("intraday_stop_fx_open_proxy", lot["ticker"], lot["event_id"], row["session"],
                  "Intraday execution order/FX is unavailable; fx_open is an explicit proxy.", "info")
        duration = (when - lot["entry_at"]).total_seconds() / 86400
        lot.update(status="closed", exit_at=when, exit_price_native=price, exit_fx=fx, exit_reason=reason,
                   proceeds_usd=proceeds, realized_pnl_usd=pnl, value_usd=0.0, unrealized_pnl_usd=0.0,
                   duration_days=duration, data_complete=proceeds is not None)
        trades.append(dict(trade_id=_hash([lot["lot_id"], when, reason, price]), lot_id=lot["lot_id"],
                           event_id=lot["event_id"], ticker=lot["ticker"], entry_at=lot["entry_at"], exit_at=when,
                           decision_at=decision, entry_price_native=lot["entry_price_native"], exit_price_native=price,
                           quantity=lot["quantity"], entry_fx=lot["entry_fx"], exit_fx=fx,
                           contribution_usd=lot["contribution_usd"], proceeds_usd=proceeds, pnl_usd=pnl,
                           return_pct=pnl / lot["contribution_usd"] * 100 if pnl is not None else None,
                           exit_reason=reason, duration_days=duration, fill_timing=timing, fx_proxy=proxy,
                           fees_usd=0.0, tax_usd=0.0))

    def unresolved(lot: dict[str, object], row: dict[str, object], code: str) -> None:
        lot["status"] = "unresolved"
        lot["_uncertain_session"] = row["session"]
        lot["data_complete"] = False
        issue(code, lot["ticker"], lot["event_id"], row["session"])

    def valuation_mark(ticker: str, day: str, cutoff: pd.Timestamp) -> tuple[dict[str, object] | None, bool]:
        if ticker not in schedules:
            return by_session.get(ticker, {}).get(day), False
        if day in expected_session_dates.get(ticker, set()):
            # A scheduled session with no actual bar/close is not a holiday.
            return by_session.get(ticker, {}).get(day), False
        coverage = calendar_ranges.get(ticker)
        if coverage is None or day < coverage[0] or day > coverage[1]:
            # An expired calendar cannot prove that an unlisted day is a
            # holiday. Missing market data must not become a stale valid mark.
            return None, False
        prior = [item for item in schedules[ticker] if item["close_at"] <= cutoff]
        if not prior:
            return None, False
        return by_session.get(ticker, {}).get(prior[-1]["session"]), True

    for when, phase, ticker, row in events:
        if phase == 0:
            for lot in ticker_lots.get(ticker, []):
                if lot["status"] != "open":
                    continue
                if config.exit_policy != "hold" and any(lot["entry_session"] < missing < row["session"]
                    for missing in unavailable_sessions.get(ticker, set())):
                    unresolved(lot, row, "unknown_intermediate_exit_path")
                    continue
                pending = lot.get("_pending")
                level = stop_level(lot)
                if pending and when > pending["at"]:
                    if row["open"] is None:
                        unresolved(lot, row, "missing_scheduled_exit_open")
                    else:
                        close_lot(lot, row, when, row["open"], pending["reason"], pending["at"], "next_open")
                elif level is not None and row["open"] is not None and row["open"] <= level:
                    close_lot(lot, row, when, row["open"], "stop_gap", when, "open_gap")
            for order in orders.get((ticker, when), []):
                if row["open"] is None or row["fx_open"] is None:
                    issue("missing_entry_price_or_fx", ticker, order["event_id"], row["session"])
                    continue
                contribution = order["amount_usd"]
                new_lot = dict(lot_id=_hash([ticker, order["event_id"], order["signal_session"], when, contribution]),
                                 event_id=order["event_id"], ticker=ticker, signal_session=order["signal_session"],
                                 observed_at=order["observed_at"], entry_at=when, entry_session=row["session"],
                                 entry_price_native=row["open"], entry_fx=row["fx_open"],
                                 quantity=contribution / (row["open"] * row["fx_open"]), contribution_usd=contribution,
                                 entry_score=_number(order.get("entry_score")), sector=order.get("sector"), status="open",
                                 exit_at=None, exit_price_native=None, exit_fx=None, exit_reason=None,
                                 proceeds_usd=None, realized_pnl_usd=None, value_usd=None, unrealized_pnl_usd=None,
                                 duration_days=None, data_complete=True, _peak=row["open"], _below=0, _pending=None)
                lots.append(new_lot)
                ticker_lots.setdefault(ticker, []).append(new_lot)
        else:
            for lot in ticker_lots.get(ticker, []):
                if lot["status"] != "open" or when <= lot["entry_at"] or row["close_at"] <= lot["entry_at"]:
                    continue
                level = stop_level(lot)
                if level is not None:
                    if row["low"] is None or row["open"] is None:
                        unresolved(lot, row, "missing_stop_path")
                        continue
                    if row["low"] <= level:
                        close_lot(lot, row, when, min(row["open"], level), "intraday_stop", when,
                                  "intraday_unknown_before_close")
                        continue
                    if config.exit_policy in {"trailing10", "original"}:
                        if row["high"] is None:
                            unresolved(lot, row, "missing_trailing_high")
                            continue
                        # Today's high cannot tighten the stop earlier in this bar.
                        lot["_peak"] = max(lot["_peak"], row["high"])
                if row["close"] is None:
                    lot["_below"] = 0
                    if config.exit_policy in {"reduce", "sell", "confirmed", "original"}:
                        unresolved(lot, row, "missing_exit_close")
                    continue
                reason = None
                if config.exit_policy in {"reduce", "sell"}:
                    if row["close"] <= lot["entry_price_native"] * (1 - config.strategy_config.stop_loss_pct / 100):
                        reason = f"close_stop{config.strategy_config.stop_loss_pct:g}"
                    elif _flag(row.get("sell_setup")) is True:
                        reason = "sell_setup"
                    elif config.exit_policy == "reduce" and _flag(row.get("reduce_setup")) is True:
                        reason = "reduce_setup"
                elif config.exit_policy == "confirmed":
                    lot["_below"] = lot["_below"] + 1 if row["sma_long"] is not None and row["close"] < row["sma_long"] else 0
                    if lot["_below"] >= 2:
                        reason = "two_closes_below_sma_long"
                elif config.exit_policy == "original":
                    if _flag(row.get("sell_setup")) is True or config.strategy_config.exit_on_reduce and _flag(row.get("reduce_setup")) is True:
                        reason = "original_setup"
                required = ("sell_setup", "reduce_setup") if config.exit_policy == "reduce" or config.exit_policy == "original" and config.strategy_config.exit_on_reduce else ("sell_setup",)
                if reason is None and config.exit_policy in {"reduce", "sell", "original"} and any(_flag(row.get(name)) is None for name in required):
                    unresolved(lot, row, "missing_exit_setup")
                    continue
                if config.exit_policy == "confirmed" and row["sma_long"] is None:
                    unresolved(lot, row, "missing_confirmation_sma")
                    continue
                if reason and lot["_pending"] is None:
                    lot["_pending"] = {"at": when, "reason": reason}

    # Build end-of-session investor wealth from actual supplied marks. No forward
    # fill of missing bars/FX, and no removal of problematic holdings from totals.
    valid_days = sorted(day for day in days if (start is None or day >= start.date().isoformat())
                        and (end is None or day <= end.date().isoformat()))
    for day in valid_days:
        cutoff = pd.Timestamp(day + "T23:59:59.999999999Z")
        if end is not None:
            cutoff = min(cutoff, end)
        existing = [lot for lot in lots if lot["entry_at"] <= cutoff]
        closed = [lot for lot in existing if lot["exit_at"] is not None and lot["exit_at"] <= cutoff]
        closed_ids = {lot["lot_id"] for lot in closed}
        active = [lot for lot in existing if lot["lot_id"] not in closed_ids]
        contributions = math.fsum(lot["contribution_usd"] for lot in existing)
        withdrawals_known = math.fsum(lot["proceeds_usd"] for lot in closed if lot["proceeds_usd"] is not None)
        realized_known = math.fsum(lot["realized_pnl_usd"] for lot in closed if lot["realized_pnl_usd"] is not None)
        withdrawals_complete = all(lot["proceeds_usd"] is not None for lot in closed)
        values = []
        for lot in active:
            mark, carried = valuation_mark(lot["ticker"], day, cutoff)
            valid = mark is not None and mark["close_available_at"] is not None and mark["close_available_at"] <= cutoff and mark["close"] is not None and mark["fx_close"] is not None
            valid = valid and mark["close_at"] >= lot["entry_at"]
            required = required_closes.get(lot["ticker"])
            if valid and required is not None and required <= cutoff and mark["close_at"] < required:
                valid = False
            uncertain_from = lot.get("_uncertain_session")
            if uncertain_from is not None and day >= uncertain_from:
                valid = False
            values.append(lot["quantity"] * mark["close"] * mark["fx_close"] if valid else None)
        open_known = math.fsum(value for value in values if value is not None)
        complete = withdrawals_complete and all(value is not None for value in values)
        value = withdrawals_known + open_known if complete else None
        daily_rows.append(dict(date=day,
            contribution_usd=math.fsum(lot["contribution_usd"] for lot in existing if lot["entry_at"].date().isoformat() == day),
            withdrawal_usd=math.fsum(lot["proceeds_usd"] for lot in closed if lot["exit_at"].date().isoformat() == day and lot["proceeds_usd"] is not None),
            contributions_usd=contributions, withdrawals_usd=withdrawals_known if withdrawals_complete else None,
            open_value_usd=open_known if all(value is not None for value in values) else None,
            open_value_known_usd=open_known, value_usd=value, value_known_usd=withdrawals_known + open_known,
            realized_pnl_usd=realized_known if withdrawals_complete else None,
            unrealized_pnl_usd=open_known - math.fsum(lot["contribution_usd"] for lot in active) if all(value is not None for value in values) else None,
            valuation_complete=complete, open_lots=len(active), unit_value_daily_approx=None, drawdown_daily_approx_pct=None))
    unit, peak, prior_value, approximable = 1.0, 1.0, 0.0, True
    for row in daily_rows:
        if not row["valuation_complete"]:
            approximable = False
        denominator = prior_value + row["contribution_usd"]
        if approximable and denominator > 0:
            unit *= row["value_usd"] / denominator
            peak = max(peak, unit)
            row["unit_value_daily_approx"] = unit
            row["drawdown_daily_approx_pct"] = (unit / peak - 1) * 100
        prior_value = row["value_usd"] if row["value_usd"] is not None else 0.0

    final = daily_rows[-1] if daily_rows else None
    final_day = final["date"] if final else None
    for lot in lots:
        if lot["status"] != "closed":
            mark, carried = valuation_mark(lot["ticker"], final_day, end) if end is not None and final_day else (None, False)
            valid = lot["status"] == "open" and mark is not None and mark["close"] is not None and mark["fx_close"] is not None and mark["close_available_at"] is not None and (end is None or mark["close_available_at"] <= end)
            valid = valid and mark["close_at"] >= lot["entry_at"]
            required = required_closes.get(lot["ticker"])
            if valid and required is not None and required <= end and mark["close_at"] < required:
                valid = False
            lot["value_usd"] = lot["quantity"] * mark["close"] * mark["fx_close"] if valid else None
            lot["unrealized_pnl_usd"] = lot["value_usd"] - lot["contribution_usd"] if valid else None
            lot["data_complete"] = valid
            lot["valuation_at"] = mark["close_at"] if valid else None
            lot["valuation_session"] = mark["session"] if valid else None
            lot["valuation_carried_closed_market"] = bool(valid and carried)
            lot["duration_days"] = (end - lot["entry_at"]).total_seconds() / 86400 if end is not None else None
            if not valid:
                issue("missing_final_valuation", lot["ticker"], lot["event_id"], final_day or "")
    filled_ids = {lot["event_id"] for lot in lots}
    unfilled = [row["event_id"] for row in eligible if row["event_id"] not in filled_ids]
    valuation_complete = bool(final is not None and final["valuation_complete"])
    input_complete = not unfilled and not any(item["severity"] == "warning" and item["code"] != "left_censored_activation" for item in issues)
    missing_valuation_days = sum(not row["valuation_complete"] for row in daily_rows)
    input_complete = input_complete and missing_valuation_days == 0
    if missing_valuation_days:
        issue("incomplete_daily_valuation", detail=f"{missing_valuation_days} sessions have incomplete investor wealth marks.")
    contributions = math.fsum(lot["contribution_usd"] for lot in lots)
    realized_values = [trade["pnl_usd"] for trade in trades]
    realized_complete = all(value is not None for value in realized_values)
    realized = math.fsum(value for value in realized_values if value is not None) if realized_complete else None
    open_values = [lot["unrealized_pnl_usd"] for lot in lots if lot["status"] != "closed"]
    latent = math.fsum(value for value in open_values if value is not None) if all(value is not None for value in open_values) else None
    final_value = final["value_usd"] if final else 0.0 if not lots else None
    complete_history = [row for row in daily_rows if row["valuation_complete"] and row["contributions_usd"] > 0]
    last_complete = complete_history[-1] if complete_history else None
    metrics = dict(contributions_usd=contributions,
        withdrawals_usd=final["withdrawals_usd"] if final else 0.0,
        open_value_usd=final["open_value_usd"] if final else 0.0,
        final_value_usd=final_value, value_usd=final_value,
        value_known_usd=final["value_known_usd"] if final else 0.0,
        withdrawals_known_usd=math.fsum(trade["proceeds_usd"] for trade in trades if trade["proceeds_usd"] is not None),
        open_value_known_usd=final["open_value_known_usd"] if final else 0.0,
        realized_pnl_known_usd=math.fsum(value for value in realized_values if value is not None),
        unrealized_pnl_known_usd=math.fsum(value for value in open_values if value is not None),
        realized_pnl_usd=realized, unrealized_pnl_usd=latent,
        profit_usd=final_value - contributions if final_value is not None else None,
        roi_pct=(final_value / contributions - 1) * 100 if final_value is not None and contributions > 0 else None,
        twr_pct=None, max_drawdown_pct=None, xirr_pct=None,
        twr_daily_approx_pct=(unit - 1) * 100 if approximable and contributions > 0 else None,
        max_drawdown_daily_approx_pct=min((row["drawdown_daily_approx_pct"] for row in daily_rows if row["drawdown_daily_approx_pct"] is not None), default=None) if approximable else None,
        n_lots=len(lots), n_trades=len(trades), n_open_lots=sum(lot["status"] != "closed" for lot in lots),
        win_rate_pct=sum(value > 0 for value in realized_values) / len(trades) * 100 if trades and realized_complete else None,
        average_duration_days=math.fsum(trade["duration_days"] for trade in trades) / len(trades) if trades else None,
        fees_usd=0.0, tax_usd=0.0, valuation_complete=valuation_complete,
        coverage_complete=input_complete and valuation_complete,
        eligible_entries=len(eligible), unfilled_entries=len(unfilled), missing_valuation_days=missing_valuation_days)
    metrics.update(last_complete_valuation_date=last_complete["date"] if last_complete else None,
        last_complete_value_usd=last_complete["value_usd"] if last_complete else None,
        last_complete_contributions_usd=last_complete["contributions_usd"] if last_complete else None,
        last_complete_roi_pct=(last_complete["value_usd"] / last_complete["contributions_usd"] - 1) * 100 if last_complete else None)
    manifest = dict(engine_version=ENGINE_VERSION, config=_safe(asdict(config)),
        input_hash=_hash({"signals": raw_records, "bars": {str(key): {"index": list(value.index), "rows": value.to_dict("records"), "attrs": value.attrs} for key, value in sorted(bars.items())}}),
        signals_received=len(signals), signals_unique=len(observations), first_session_observations=len(first),
        eligible_event_ids=[row["event_id"] for row in eligible], filled_event_ids=sorted(filled_ids), unfilled_event_ids=unfilled,
        pending_exit_orders=_safe([dict(lot_id=lot["lot_id"], ticker=lot["ticker"],
            decision_at=lot["_pending"]["at"], reason=lot["_pending"]["reason"],
            intended_open_at=next((item["open_at"] for item in schedules.get(lot["ticker"], [])
                if item["open_at"] > lot["_pending"]["at"]), None))
            for lot in lots if lot.get("_pending")]),
        eligible_tickers=sorted({row["ticker"] for row in eligible}), missing_tickers=sorted(missing_tickers),
        blocked_tickers=sorted(blocked), quarantined_from=quarantines, coverage_complete=metrics["coverage_complete"],
        entry_rule="Exactly Entrada fuerte; first observation per ticker/signal_session; first open_at strictly after observed_at.",
        sizing_rule="fixed=amount; tiered=amount*(1 for75-79,2 for80-84,3 for85-100); factors=tiered*min(company,valuation,opportunity,risk)/100; all four historical factors required.",
        wealth_rule="Contributions are external. Sale proceeds are withdrawn, never reused, and remain segregated zero-yield cash in total investor wealth.",
        limitations=["No costs, slippage, taxes, dividends or portfolio capital constraints are simulated.",
                     "Intraday stop fills use fx_open as an explicit FX proxy; OHLC cannot identify their execution timestamp.",
                     "Exact TWR, exact unitized drawdown and XIRR are not computed. Named daily proxies assume all contributions at day start; no bridging missing valuations.",
                     "Missing expected bars/close/FX are not forward-filled. Only an authoritative session_schedule can prove a market closed and permit carrying its last supplied close/FX mark, with valuation_at retained.",
                     "Original means the existing configured exit rules, not the original cash-constrained position allocator."])
    return AuditResult(pd.DataFrame(daily_rows, columns=DAILY_COLUMNS).set_index("date"),
                       pd.DataFrame(lots, columns=LOT_COLUMNS), pd.DataFrame(trades, columns=TRADE_COLUMNS),
                       pd.DataFrame(issues, columns=ISSUE_COLUMNS), metrics, manifest)
