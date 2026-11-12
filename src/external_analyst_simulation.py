"""Independent, point-in-time paper portfolios for externally observed ideas.

This is not a signal engine and never accesses a broker, network or database.
OHLC bars are evidence, not ticks: a conditional entry touched inside a bar is
only known when the bar is available. Proceeds from an intrabar exit cannot be
reused at an earlier opening. Both a stop and target in one bar use a documented
stop-first *conservative estimate*, flagged and excluded from the hit rate.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import ipaddress
import json
import math
from typing import Mapping
from urllib.parse import urlparse


SIMULATION_VERSION = "external-paper-v1"
STRATEGIES = {
    "A": "Resultados empresariales",
    "B": "Beneficiarios indirectos de noticias y resultados",
    "C": "Rebote o ruptura técnica",
    "D": "Analistas externos",
}
_TERMINAL_SOURCE = {
    "target_reached", "stopped", "expired", "cancelled", "historical", "objetivo alcanzado",
    "stop ejecutado", "caducada", "anulada",
}


@dataclass(frozen=True)
class SimulationCosts:
    """Estimated EUR fees, not a claim about any user's broker tariff."""

    commission_eur: float = 1.0
    spread_bps: float = 10.0
    slippage_bps: float = 5.0
    fx_fee_bps: float = 15.0

    def __post_init__(self):
        for field, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid simulated cost: {field}")
        if max(self.spread_bps, self.slippage_bps, self.fx_fee_bps) >= 1000:
            raise ValueError("Cost rates must be below 1000 basis points")


@dataclass(frozen=True)
class SimulationConfig:
    initial_cash_eur: float = 100.0
    fractional_shares: bool = True
    ambiguity_policy: str = "conservative_stop_first"

    def __post_init__(self):
        if not _positive(self.initial_cash_eur):
            raise ValueError("The paper portfolio needs a positive finite EUR budget")
        if self.ambiguity_policy != "conservative_stop_first":
            raise ValueError("Only the explicit conservative stop-first OHLC policy is supported")


def _clock(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("An aware ISO timestamp is required") from error
    else:
        raise ValueError("An aware ISO timestamp is required")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Naive timestamps cannot establish when evidence was available")
    return parsed.astimezone(timezone.utc)


def _iso(value):
    return _clock(value).isoformat()


def _positive(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def _number(value):
    if isinstance(value, bool):
        raise ValueError("A boolean is not a price")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError("A numeric price is required") from error
    if not math.isfinite(number):
        raise ValueError("Non-finite values are not prices")
    return number


def _id(*parts):
    return sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str,
                             separators=(",", ":")).encode()).hexdigest()


def _forecast_fingerprint(record):
    # Repeated delivery is not a new forecast. Later changes remain evidence
    # history, never edits to an already frozen entry/stop/target.
    return _id({key: value for key, value in record.items()
                if key not in {"received_at", "observed_at"}})


def _observation_order(record):
    try:
        known = max(_clock(record["received_at"]), _clock(record["observed_at"]))
    except (KeyError, ValueError, TypeError):
        known = datetime.max.replace(tzinfo=timezone.utc)
    return known, str(record.get("id") or record.get("call_id") or ""), _id(record)


def _public_evidence(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
        host = parsed.hostname or ""
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        return (parsed.scheme == "https" and bool(host) and "." in host
                and not host.endswith((".local", ".localhost", ".internal"))
                and not parsed.username and not parsed.password and literal is None)
    except ValueError:
        return False


def _call(record: Mapping, cutoff: datetime) -> dict:
    call = dict(record)
    identity = str(call.get("id") or call.get("call_id") or "")
    if not identity:
        raise ValueError("A stable recommendation id is required")
    call["id"] = identity
    if not call.get("ticker") or not call.get("currency"):
        raise ValueError("Ticker and quote currency must be explicit")
    call["ticker"] = str(call["ticker"]).strip().upper()
    raw_currency = str(call["currency"]).strip()
    if raw_currency in {"GBp", "GBX"}:
        raise ValueError("A pence quote needs an explicit unit conversion, not a GBP substitution")
    call["currency"] = raw_currency.upper()
    call["known_at"] = max(_clock(call["received_at"]), _clock(call["observed_at"]))
    if call.get("published_at") and _clock(call["published_at"]) > call["known_at"]:
        raise ValueError("The publication clock is later than the observation")
    call["future"] = call["known_at"] > cutoff
    call["analyst"] = str(call.get("analyst") or "Analista no indicado")
    call["expires"] = _clock(call["expires_at"]) if call.get("expires_at") else None
    if call.get("source_status", "unknown") in _TERMINAL_SOURCE:
        call["historical"] = True
        return call
    call["historical"] = False
    if call.get("action") != "buy" or call.get("direction") not in {"long", "alcista"}:
        raise ValueError("Only explicit long purchase ideas are simulated; no short sales")
    if call.get("executable", True) is not True:
        raise ValueError("The source recommendation is not executable")
    rule = call.get("entry_rule")
    if rule not in {"limit", "breakout", "market"}:
        raise ValueError("The source does not specify a verifiable entry rule")
    for name in ("entry", "stop", "target"):
        call[name] = _number(call.get(name))
    if not 0 < call["stop"] < call["entry"] < call["target"]:
        raise ValueError("A long idea needs 0 < stop < entry < target")
    if call.get("reference_price") is not None:
        reference = _number(call["reference_price"])
        if not call["stop"] < reference < call["target"]:
            raise ValueError("The observed source price is already outside the original risk/target")
    return call


def _records(bars):
    """Accept plain JSON records or a ticker -> records mapping."""
    if isinstance(bars, Mapping):
        for ticker, records in bars.items():
            if hasattr(records, "to_dict"):
                records = records.to_dict("records")
            for record in records:
                yield {"ticker": ticker, **dict(record)}
    else:
        if hasattr(bars, "to_dict"):
            bars = bars.to_dict("records")
        yield from (dict(record) for record in bars)


def _bar(record, cutoff):
    bar = dict(record)
    bar["ticker"] = str(bar.get("ticker") or "").strip().upper()
    if not bar["ticker"]:
        raise ValueError("A quote needs its ticker")
    for key in ("open_at", "close_at", "available_at"):
        bar[key] = _clock(bar[key])
    if not bar["open_at"] < bar["close_at"] <= bar["available_at"]:
        raise ValueError("A completed bar needs open < close <= available")
    if bar["available_at"] > cutoff:
        return None
    if bar.get("data_issue"):
        raise ValueError("This session has an explicit data-coverage incident")
    if any(_number(bar.get(key, 0) or 0) not in {0., 1.}
           for key in ("stock_splits", "split", "split_ratio")):
        raise ValueError("A split needs an explicit point-in-time position adjustment")
    for key in ("open", "high", "low", "close"):
        bar[key] = _number(bar.get(key))
    if (min(bar[key] for key in ("open", "high", "low", "close")) <= 0
            or bar["high"] < max(bar["open"], bar["close"])
            or bar["low"] > min(bar["open"], bar["close"]) or bar["low"] > bar["high"]):
        raise ValueError("The OHLC prices are not coherent")
    raw_currency = str(bar.get("currency") or "").strip()
    if raw_currency in {"GBp", "GBX"}:
        raise ValueError("A pence quote cannot be silently converted to GBP")
    bar["currency"] = raw_currency.upper()
    if not bar["currency"]:
        raise ValueError("The quote currency must be explicit")
    if bar["currency"] == "EUR":
        supplied = bar.get("fx_eur_per_unit", 1.0)
        if _number(supplied) != 1.0:
            raise ValueError("An EUR quote cannot have a non-unit EUR conversion")
        bar["fx_eur_per_unit"] = 1.0
    else:
        fx = _number(bar.get("fx_eur_per_unit"))
        if fx <= 0 or _clock(bar.get("fx_observed_at")) > bar["open_at"]:
            raise ValueError("A known positive historical FX rate is required before opening")
        bar["fx_eur_per_unit"] = fx
    return bar


def _drawdown(curve):
    peak, maximum = 0.0, 0.0
    for point in curve:
        value = point["nav_eur"]
        peak = max(peak, value)
        if peak:
            maximum = max(maximum, (peak - value) / peak * 100)
    return maximum


def _metrics(trades, positions, curve, initial, costs_paid, maximum_invested, statuses):
    verified = [trade for trade in trades if trade["verified"]]
    winners = [trade for trade in verified if trade["pnl_eur"] > 0]
    losers = [trade for trade in verified if trade["pnl_eur"] < 0]
    gain = sum(trade["pnl_eur"] for trade in winners)
    loss = -sum(trade["pnl_eur"] for trade in losers)
    realized = sum(trade["pnl_eur"] for trade in trades)
    latent = sum(position["mark_eur"] - position["cost_eur"] for position in positions)
    nav = curve[-1]["nav_eur"] if curve else initial
    mean = lambda items, field: sum(item[field] for item in items) / len(items) if items else None
    return {
        "initial_cash_eur": initial, "net_profit_eur": nav - initial,
        "net_return_pct": (nav / initial - 1) * 100,
        "realized_pnl_eur": realized, "unrealized_pnl_eur": latent,
        "closed_trades": len(trades), "verified_closed_trades": len(verified),
        "ambiguous_trades": len(trades) - len(verified),
        "hit_rate_pct": len(winners) / len(verified) * 100 if verified else None,
        "mean_gain_eur": mean(winners, "pnl_eur"), "mean_loss_eur": mean(losers, "pnl_eur"),
        "mean_gain_pct": mean(winners, "return_pct"), "mean_loss_pct": mean(losers, "return_pct"),
        "profit_factor": gain / loss if loss else None,
        "mean_duration_days": mean(trades, "duration_days"),
        "max_drawdown_pct": _drawdown(curve), "total_estimated_costs_eur": costs_paid,
        "turnover_eur": sum(trade["entry_gross_eur"] + trade["exit_gross_eur"] for trade in trades)
                          + sum(position["entry_gross_eur"] for position in positions),
        "max_simultaneously_committed_eur": maximum_invested,
        "status_counts": dict(sorted(Counter(statuses.values()).items())),
        "return_basis": "EUR cash plus marked positions, conservative ambiguous exits; not sum of call percentages",
        "costs_are_estimates": True,
        "reliable": (bool(verified) and all(trade["verified"] for trade in trades)
                     and all(position.get("verified", True) for position in positions)),
    }


def simulate_external_calls(calls, bars, *, as_of, initial_cash_eur=100.0,
                            costs: SimulationCosts | None = None,
                            config: SimulationConfig | None = None,
                            _include_analysts=True) -> dict:
    """Run a long-only cash-limited portfolio from observations, never forecasts.

    All fees are estimates. Fractional shares are a paper assumption. Known
    source-terminal calls are excluded, not profitable historical trades. A
    missing or contradictory quote/FX row remains an incident, never a fill.
    """
    cutoff = _clock(as_of)
    costs = costs or SimulationCosts()
    config = config or SimulationConfig(initial_cash_eur=initial_cash_eur)
    initial = float(config.initial_cash_eur)
    raw_calls = [dict(call) for call in calls]
    observed_calls, analysts = [], set()
    events, incidents, trades, accepted, statuses = [], [], [], {}, {}
    seen = {}
    raw_seen = set()

    def incident(code, identity=None, ticker=None):
        row = {"code": code, "call_id": identity, "ticker": ticker}
        if row not in incidents:
            incidents.append(row)

    for raw in sorted(raw_calls, key=_observation_order):
        identity = str(raw.get("id") or raw.get("call_id") or "")
        # Drop future observations before inspecting prices, source state or
        # revisions. They cannot alter today's metrics, incidents or identity.
        known = None
        try:
            known = max(_clock(raw["received_at"]), _clock(raw["observed_at"]))
            if known > cutoff:
                continue
        except (KeyError, ValueError, TypeError):
            pass
        fingerprint = _id(raw)
        if fingerprint in raw_seen:
            continue
        raw_seen.add(fingerprint)
        observed_calls.append(raw)
        forecast = _forecast_fingerprint(raw)
        if identity in seen:
            first_known, first_forecast = seen[identity]
            if known is None:
                incident("revision_clock_unverified_frozen_call_unchanged", identity, raw.get("ticker"))
            elif known > first_known:
                if forecast != first_forecast:
                    incident("later_revision_not_applied_to_frozen_trade", identity, raw.get("ticker"))
            elif forecast != first_forecast:
                accepted.pop(identity, None)
                statuses[identity] = "not_verifiable"
                incident("conflicting_revisions_need_point_in_time_history", identity, raw.get("ticker"))
            continue
        try:
            call = _call(raw, cutoff)
        except (ValueError, KeyError, TypeError):
            statuses[identity or _id(raw)] = "not_verifiable"
            incident("invalid_or_incomplete_call", identity or None, raw.get("ticker"))
            continue
        if call["future"]:
            continue
        analysts.add(call["analyst"])
        seen[identity] = (call["known_at"], forecast)
        if call["historical"]:
            statuses[identity] = "historical_not_executable"
            incident("source_terminal_before_observation", identity, call["ticker"])
        else:
            accepted[identity] = call
            statuses[identity] = "entry_pending"

    clean, duplicate_keys, barriers = {}, set(), {}
    quote_records = list(_records(bars))
    relevant = {call["ticker"] for call in accepted.values()}
    ticker_start = {ticker: min(call["known_at"] for call in accepted.values()
                               if call["ticker"] == ticker) for ticker in relevant}

    def invalidate_path(raw):
        ticker = str(raw.get("ticker") or "").strip().upper()
        try:
            if _clock(raw["available_at"]) > cutoff:
                return
            if _clock(raw["close_at"]) < ticker_start[ticker]:
                return
            barrier = _clock(raw["open_at"])
        except (ValueError, KeyError, TypeError):
            barrier = ticker_start[ticker]
        barriers[ticker] = min(barriers.get(ticker, barrier), barrier)

    for raw in quote_records:
        if str(raw.get("ticker") or "").strip().upper() not in relevant:
            continue
        # Even malformed future candles cannot change today's incidents or
        # invalidate a path that was already verifiable at the cutoff.
        try:
            if _clock(raw["available_at"]) > cutoff:
                continue
        except (ValueError, KeyError, TypeError):
            try:
                if _clock(raw["open_at"]) > cutoff:
                    continue
            except (ValueError, KeyError, TypeError):
                pass
        try:
            bar = _bar(raw, cutoff)
            if bar is None:
                continue
            key = (bar["ticker"], bar["open_at"])
            if key in clean:
                if _id(clean[key]) != _id(bar):
                    duplicate_keys.add(key)
                    incident("conflicting_quote_rows", ticker=bar["ticker"])
                    invalidate_path(raw)
                continue
            clean[key] = bar
        except (ValueError, KeyError, TypeError):
            incident("invalid_quote_or_historical_fx", ticker=raw.get("ticker"))
            invalidate_path(raw)
    for key in duplicate_keys:
        clean.pop(key, None)
    timeline = []
    start = min([call["known_at"] for call in accepted.values()] + [cutoff])
    for key, bar in clean.items():
        if bar["ticker"] in barriers and bar["open_at"] >= barriers[bar["ticker"]]:
            continue
        if bar["available_at"] < start:
            incident("pre_observation_prices_ignored", ticker=bar["ticker"])
            continue
        timeline.append((bar["open_at"], 1, key, "open", bar))
        timeline.append((bar["available_at"], 0, key, "close", bar))
    timeline.sort(key=lambda item: (item[0], item[1], item[2]))

    positions, orders = {}, {}
    cash, costs_paid, maximum_invested = initial, 0.0, 0.0
    curve = [{"at": min([start] + [item[0] for item in timeline]).isoformat(),
              "cash_eur": cash, "reserved_eur": 0.0, "positions_eur": 0.0, "nav_eur": initial}]

    def event(kind, call, at, **data):
        payload = {"event_type": kind, "call_id": call["id"], "ticker": call["ticker"],
                   "at": at.isoformat(), "known_at": call["known_at"].isoformat(), **data}
        payload["event_id"] = _id(SIMULATION_VERSION, payload)
        events.append(payload)

    def equity(at):
        nonlocal maximum_invested
        reserved = sum(order["reserved_eur"] for order in orders.values())
        position_value = sum(position["mark_eur"] for position in positions.values())
        invested = reserved + sum(position["cost_eur"] for position in positions.values())
        maximum_invested = max(maximum_invested, invested)
        value = cash + reserved + position_value
        if cash < -1e-7 or value < -1e-7:
            raise AssertionError("Paper cash cannot become negative")
        curve.append({"at": at.isoformat(), "cash_eur": max(0., cash), "reserved_eur": reserved,
                      "positions_eur": position_value, "nav_eur": max(0., value)})

    def fill_entry(call, bar, raw_price, at, available_at, reserved=None, at_open=True):
        nonlocal cash, costs_paid
        fx = bar["fx_eur_per_unit"]
        bps = costs.spread_bps / 2 + costs.slippage_bps
        effective = raw_price * (1 + bps / 10000)
        fx_multiplier = 1 + (costs.fx_fee_bps / 10000 if call["currency"] != "EUR" else 0)
        budget = cash if reserved is None else reserved
        if raw_price <= call["stop"] or effective >= call["target"] or budget <= costs.commission_eur * 2:
            if reserved is not None:
                cash += reserved
            statuses[call["id"]] = "not_verifiable" if raw_price <= call["stop"] else "entry_pending"
            incident("entry_gap_outside_original_risk_or_insufficient_cash", call["id"], call["ticker"])
            return
        # Reserve enough cash for a later exit commission, even after a loss.
        spendable = budget - costs.commission_eur * 2
        quantity = spendable / (effective * fx * fx_multiplier)
        if not config.fractional_shares:
            quantity = math.floor(quantity)
        if quantity <= 0:
            if reserved is not None:
                cash += reserved
            incident("budget_cannot_buy_one_share", call["id"], call["ticker"])
            return
        gross = quantity * raw_price * fx
        cost = quantity * effective * fx * fx_multiplier + costs.commission_eur
        if reserved is None:
            cash -= cost
        else:
            cash += reserved - cost
        fee = cost - gross
        costs_paid += fee
        positions[call["id"]] = {
            "call": call, "quantity": quantity, "cost_eur": cost,
            "entry_gross_eur": gross, "entry_price_native": raw_price,
            "entry_fx": fx, "entry_at": at, "entry_available_at": available_at,
            "entry_at_open": at_open, "entry_bar": (bar["ticker"], bar["open_at"]),
            "mark_eur": quantity * raw_price * fx,
        }
        statuses[call["id"]] = "open"
        event("entry_activated", call, available_at, executed_at=at.isoformat(), price_native=raw_price,
              fx_eur_per_unit=fx, quantity=quantity, cost_eur=cost, costs_eur=fee,
              intrabar_time_unknown=not at_open)

    def fill_exit(position, bar, raw_price, at, available_at, reason, *, verified=True):
        nonlocal cash, costs_paid
        call, quantity = position["call"], position["quantity"]
        # A possible earlier target on an unknown intrabar entry path also
        # makes the later exit time/price uncertain, even if this bar is clear.
        verified = verified and position.get("verified", True)
        fx = bar["fx_eur_per_unit"]
        multiplier = 1 - (costs.spread_bps / 2 + costs.slippage_bps) / 10000
        fx_multiplier = 1 - (costs.fx_fee_bps / 10000 if call["currency"] != "EUR" else 0)
        gross = quantity * raw_price * fx
        proceeds = quantity * raw_price * multiplier * fx * fx_multiplier - costs.commission_eur
        # Exit fee was retained at purchase; external margin is never needed.
        cash += proceeds
        costs_paid += gross - proceeds
        pnl = proceeds - position["cost_eur"]
        trade = {
            "call_id": call["id"], "ticker": call["ticker"], "analyst": call["analyst"],
            "entry_at": position["entry_at"].isoformat(), "exit_at": at.isoformat(),
            "entry_available_at": position["entry_available_at"].isoformat(),
            "exit_available_at": available_at.isoformat(), "quantity": quantity,
            "entry_price_native": position["entry_price_native"], "exit_price_native": raw_price,
            "entry_fx_eur_per_unit": position["entry_fx"], "exit_fx_eur_per_unit": fx,
            "entry_gross_eur": position["entry_gross_eur"], "exit_gross_eur": gross,
            "entry_cost_eur": position["cost_eur"], "exit_proceeds_eur": proceeds,
            "pnl_eur": pnl, "return_pct": pnl / position["cost_eur"] * 100,
            "duration_days": (at - position["entry_at"]).total_seconds() / 86400,
            "status": reason, "verified": verified,
        }
        trades.append(trade)
        statuses[call["id"]] = reason
        positions.pop(call["id"])
        event("exit", call, available_at, executed_at=at.isoformat(), status=reason,
              price_native=raw_price, pnl_eur=pnl, verified=verified)

    def compatible(call, bar):
        if call["currency"] != bar["currency"]:
            incident("quote_currency_mismatch", call["id"], call["ticker"])
            return False
        return True

    for when, _priority, key, phase, bar in timeline:
        if phase == "open":
            # Gap orders existed before this opening. Their cash may be reused
            # now, unlike proceeds only inferred from later high/low evidence.
            for position in list(positions.values()):
                call = position["call"]
                if call["ticker"] != bar["ticker"] or not compatible(call, bar):
                    continue
                if bar["open"] <= call["stop"]:
                    fill_exit(position, bar, bar["open"], when, when, "stop_executed")
                elif bar["open"] >= call["target"]:
                    fill_exit(position, bar, bar["open"], when, when, "target_reached")
            for call in sorted(accepted.values(), key=lambda row: (row["known_at"], row["id"])):
                identity = call["id"]
                if call["ticker"] != bar["ticker"] or statuses[identity] != "entry_pending":
                    continue
                if when <= call["known_at"]:
                    incident("opening_not_after_observation", identity, call["ticker"])
                    continue
                if call["expires"] and when >= call["expires"]:
                    statuses[identity] = "expired"
                    event("expired", call, when)
                    continue
                if not compatible(call, bar) or any(order["call"]["ticker"] == call["ticker"] for order in orders.values()):
                    continue
                if any(position["call"]["ticker"] == call["ticker"] for position in positions.values()):
                    continue
                if cash <= costs.commission_eur * 2:
                    incident("cash_budget_in_use", identity, call["ticker"])
                    continue
                if call["entry_rule"] == "market" or (
                    call["entry_rule"] == "limit" and bar["open"] <= call["entry"]
                    or call["entry_rule"] == "breakout" and bar["open"] >= call["entry"]
                ):
                    fill_entry(call, bar, bar["open"], when, when)
                else:
                    reserved = cash
                    cash -= reserved
                    orders[identity] = {"call": call, "reserved_eur": reserved, "bar": key}
                    event("entry_order_reserved", call, when, reserved_eur=reserved)
        else:
            for identity, order in list(orders.items()):
                if order["bar"] != key:
                    continue
                call = order["call"]
                del orders[identity]
                if call["expires"] and call["expires"] < bar["close_at"]:
                    cash += order["reserved_eur"]
                    statuses[identity] = "not_verifiable"
                    incident("intrabar_expiry_order_unknown", identity, call["ticker"])
                elif bar["low"] <= call["entry"] <= bar["high"]:
                    fill_entry(call, bar, call["entry"], bar["close_at"], when,
                               order["reserved_eur"], at_open=False)
                else:
                    cash += order["reserved_eur"]
            for position in list(positions.values()):
                call = position["call"]
                if call["ticker"] != bar["ticker"] or not compatible(call, bar):
                    continue
                position["mark_eur"] = position["quantity"] * bar["close"] * bar["fx_eur_per_unit"]
                stop_hit, target_hit = bar["low"] <= call["stop"], bar["high"] >= call["target"]
                entered_intrabar = position["entry_bar"] == key and not position["entry_at_open"]
                if stop_hit and target_hit or entered_intrabar and stop_hit:
                    incident("ohlc_exit_order_ambiguous_conservative_stop", call["id"], call["ticker"])
                    fill_exit(position, bar, call["stop"], bar["close_at"], when,
                              "ambiguous_conservative_stop", verified=False)
                elif entered_intrabar and target_hit:
                    incident("entry_and_target_same_bar_not_counted_as_win", call["id"], call["ticker"])
                    position["verified"] = False
                elif stop_hit:
                    fill_exit(position, bar, call["stop"], bar["close_at"], when, "stop_executed")
                elif target_hit:
                    fill_exit(position, bar, call["target"], bar["close_at"], when, "target_reached")
        equity(when)

    for call in accepted.values():
        if call["ticker"] in barriers and statuses[call["id"]] in {"entry_pending", "open"}:
            statuses[call["id"]] = "not_verifiable"
            incident("price_path_incomplete_no_later_exit_inferred", call["id"], call["ticker"])
            if call["id"] in positions:
                positions[call["id"]]["verified"] = False
                positions[call["id"]]["mark_is_stale"] = True
        if statuses[call["id"]] == "entry_pending":
            if call["expires"] and cutoff >= call["expires"]:
                statuses[call["id"]] = "expired"
            elif not any(bar["ticker"] == call["ticker"] and bar["currency"] == call["currency"]
                         and bar["open_at"] > call["known_at"]
                         for bar in clean.values()):
                statuses[call["id"]] = "not_verifiable"
                incident("no_eligible_subsequent_prices", call["id"], call["ticker"])
    equity(cutoff)
    open_positions = [
        {key: value for key, value in position.items() if key not in {"call", "entry_bar"}}
        | {"call_id": identity, "ticker": position["call"]["ticker"],
           "analyst": position["call"]["analyst"], "entry_at": position["entry_at"].isoformat(),
           "entry_available_at": position["entry_available_at"].isoformat()}
        for identity, position in sorted(positions.items())
    ]
    metrics = _metrics(trades, open_positions, curve, initial, costs_paid, maximum_invested, statuses)
    if barriers:
        metrics["reliable"] = False
    by_analyst = {}
    if _include_analysts:
        for analyst in sorted(analysts):
            subset = [raw for raw in observed_calls if str(raw.get("analyst") or "Analista no indicado") == analyst]
            own = simulate_external_calls(subset, quote_records, as_of=cutoff,
                costs=costs, config=config, _include_analysts=False)
            by_analyst[analyst] = {"basis": f"independent EUR{initial:g} paper account; do not sum analysts",
                                  **own["metrics"],
                                  "N_observed": own["coverage"]["distinct_observed_calls"],
                                  "N_executable": own["coverage"]["executable_observed_calls"],
                                  "verification_coverage_pct": (
                                      own["metrics"]["verified_closed_trades"] / own["coverage"]["distinct_observed_calls"] * 100
                                      if own["coverage"]["distinct_observed_calls"] else None),
                                  "open_positions": len(own["open_positions"]),
                                  "small_sample": own["metrics"]["verified_closed_trades"] < 30}
    canonical = {
        "version": SIMULATION_VERSION, "as_of": cutoff.isoformat(),
        "costs": asdict(costs), "config": asdict(config),
        "calls": sorted(observed_calls, key=lambda row: (str(row.get("id")), _id(row))),
        "bars": sorted(clean.values(), key=lambda bar: (bar["ticker"], bar["open_at"])),
        "price_path_barriers": {ticker: value.isoformat() for ticker, value in sorted(barriers.items())},
    }
    return {
        "simulation_id": _id(canonical), "version": SIMULATION_VERSION,
        "as_of": cutoff.isoformat(), "initial_cash_eur": initial, "cash_eur": cash,
        "reserved_eur": sum(order["reserved_eur"] for order in orders.values()),
        "nav_eur": curve[-1]["nav_eur"], "events": events, "trades": trades,
        "open_positions": open_positions, "equity_curve": curve, "statuses": statuses,
        "metrics": metrics, "by_analyst": by_analyst, "incidents": incidents,
        "coverage": {"received_calls": len(observed_calls), "distinct_observed_calls": len(statuses),
                     "executable_observed_calls": len(accepted), "usable_price_bars": len(clean),
                     "blocked_price_paths": len(barriers),
                     "not_verifiable": sum(status == "not_verifiable" for status in statuses.values())},
        "limitations": [
            "Paper simulation only: no broker instructions and no guaranteed fills.",
            "Fractional shares and estimated costs; original analysis rules are unchanged.",
            "Daily OHLC cannot reveal intrabar order: ambiguous exits use a flagged conservative stop, never a win.",
            "No completed available candle or historical currency evidence means no verifiable execution.",
            "Entry, stop and target are frozen at the first recorded observation; later source amendments are separate history, not retroactive trade changes.",
            "A missing or split session blocks later inferred exits; an existing position remains marked stale, never an assumed later win.",
        ],
        "is_real_execution": False,
    }


def simulate_four_strategies(candidates, bars, *, as_of, costs: SimulationCosts | None = None,
                             config: SimulationConfig | None = None) -> dict:
    """Four equivalent EUR100 portfolios; missing A/B evidence stays missing."""
    config = config or SimulationConfig()
    if float(config.initial_cash_eur) != 100.0:
        raise ValueError("The four comparable strategy accounts each start with exactly EUR100")
    grouped, evidence_errors = defaultdict(list), defaultdict(list)
    for raw in candidates:
        call = dict(raw)
        strategy = str(call.get("strategy") or ("D" if call.get("provider") else ""))
        if strategy not in STRATEGIES:
            raise ValueError("Every candidate needs one of the four declared strategies")
        try:
            known = max(_clock(call["received_at"]), _clock(call["observed_at"]))
            if known > _clock(as_of):
                continue
            if strategy in {"A", "B"}:
                if not _public_evidence(call.get("evidence_url")) or _clock(call.get("evidence_at")) > known:
                    raise ValueError("Missing contemporaneous public evidence")
                if strategy == "B" and not str(call.get("beneficiary_relation") or "").strip():
                    raise ValueError("An indirect beneficiary needs a documented relationship")
            elif strategy == "C":
                if not call.get("engine_snapshot_id") or _clock(call.get("signal_known_at")) > known:
                    raise ValueError("Missing a contemporaneous frozen technical observation")
            elif not call.get("provider"):
                raise ValueError("An external analyst needs a declared provider")
        except (KeyError, ValueError, TypeError):
            evidence_errors[strategy].append(str(call.get("id") or "unidentified"))
            continue
        grouped[strategy].append(call)
    # Materialize once: generator inputs must be identical across all accounts.
    quote_records = list(_records(bars))
    accounts = {}
    for strategy, title in STRATEGIES.items():
        result = simulate_external_calls(grouped[strategy], quote_records, as_of=as_of,
                                         costs=costs, config=config)
        result.update(strategy=strategy, strategy_name=title,
                      status="simulated" if grouped[strategy] else "awaiting_evidence",
                      evidence_rejected=len(evidence_errors[strategy]))
        accounts[strategy] = result
    return {"version": SIMULATION_VERSION, "as_of": _iso(as_of), "accounts": accounts,
            "costs": asdict(costs or SimulationCosts()), "initial_cash_per_account_eur": 100.0,
            "total_initial_cash_eur": 400.0, "is_real_execution": False,
            "limitations": ["No retrospective winner selection; each strategy uses its independently supplied contemporaneous evidence."]}
