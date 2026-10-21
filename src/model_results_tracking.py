"""Prospective USD experiment, independent of the real portfolio and SMTP.

Every input is first-seen immutable. Delayed collectors move the knowledge clock
forward, never trade backwards. Replaying the frozen feed is a calculation;
only orders recorded before their intended opening may become recorded fills.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import subprocess

import pandas as pd

from config import StrategyConfig
from src.model_results_engine import ExecutionCosts, FROZEN_MODELS, run_frozen_models, runtime_versions
from src.model_results_store import (GENESIS_HASH, content_hash, create_model_results_store,
    deterministic_event_id, ModelResultsConflict)
from src.strategy_audit_data import normalize_audit_signals
from src.strategy_audit_engine import _safe


TRACKING_VERSION = "prospective-frozen-feed-v1"
_ROOT = Path(__file__).resolve().parents[1]
_ENGINE_FILES = ("config.py", "src/indicators.py", "src/signal_engine.py",
    "src/position_evaluator.py", "src/backtesting.py", "src/strategy_audit_engine.py",
    "src/strategy_audit_data.py", "src/strategy_audit_service.py", "src/model_results_engine.py",
    "src/model_results_service.py", "src/model_results_store.py", "src/model_results_tracking.py",
    "src/price_units.py", "src/instruments.py", "src/analysis_policy.py")


def engine_identity() -> dict:
    hashes = {name: hashlib.sha256((_ROOT / name).read_bytes()).hexdigest()
              for name in _ENGINE_FILES if (_ROOT / name).is_file()}
    runtime = runtime_versions()
    return {"files": hashes, "strategy_config": asdict(StrategyConfig()), "runtime_versions": runtime,
            "signature": content_hash([hashes, asdict(StrategyConfig())])}


def git_revision() -> str:
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_ROOT,
            stderr=subprocess.DEVNULL, timeout=5, text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=_ROOT,
            stderr=subprocess.DEVNULL, timeout=5, text=True).strip()
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _clock(value=None) -> pd.Timestamp:
    stamp = pd.Timestamp(value if value is not None else datetime.now(timezone.utc))
    if stamp.tzinfo is None:
        raise ValueError("An explicit UTC-aware collection timestamp is required")
    return stamp.tz_convert("UTC")


def _event(kind: str, key: object, when, payload: dict, *, available=None) -> dict:
    occurred = _clock(when)
    return {"event_type": kind, "event_id": deterministic_event_id(kind, key),
            "occurred_at": occurred.isoformat(),
            "available_at": _clock(available if available is not None else occurred).isoformat(),
            "payload": _safe(payload)}


def _append(store, season_id, *, run_id, now, events=(), snapshot=None):
    batches = store.select("batches", season_id=season_id)
    head = batches[-1]["head_hash"] if batches else GENESIS_HASH
    return store.append_run(season_id, run_id=run_id, market_date=_clock(now).date().isoformat(),
        events=events, snapshot=snapshot, expected_previous_hash=head)


def activate_model_experiment(journal, audit_manifest: dict, *, now=None,
        source="technical", budget_usd=6000.0, costs: ExecutionCosts | None = None) -> str:
    """Explicit activation after a calculated audit; never called at import/load.

    Audit coverage is evidence, not a permission to claim profitability. The
    complete audit manifest is hashed, not copied with private owner records.
    Theory starts with NAV/cash zero and receives USD100 per accepted entry.
    The lot amount is never mislabelled as initial funded capital.
    """
    if not audit_manifest or not isinstance(audit_manifest, dict):
        raise ValueError("Calculate and retain a retrospective audit manifest first")
    if source not in {"technical", "growth"} or not math.isfinite(budget_usd) or budget_usd < 100:
        raise ValueError("Invalid source or virtual budget")
    when, costs = _clock(now), costs or ExecutionCosts()
    costs.validate()
    identity = engine_identity()
    experiment_id = content_hash([source, when.isoformat(), content_hash(audit_manifest), identity["signature"]])
    store = create_model_results_store(journal)
    for mode in ("theoretical", "budget"):
        for model, spec in FROZEN_MODELS.items():
            store.create_season(dict(season_key=experiment_id, strategy_key=model, mode=mode,
                base_currency="USD", start_date=when.date().isoformat(),
                activation_at=when.isoformat(), initial_nav_usd=budget_usd if mode == "budget" else 0.0,
                initial_cash_usd=budget_usd if mode == "budget" else 0.0,
                funding_rule="initial_fixed" if mode == "budget" else "external_100_per_entry",
                engine_version=TRACKING_VERSION, experiment_id=experiment_id, source=source,
                frozen_spec=asdict(spec), execution_costs=asdict(costs), budget_usd=budget_usd,
                engine_identity=identity, git_sha=git_revision(),
                audit_manifest_hash=content_hash(audit_manifest),
                cost_label="Costes estimados configurables; no tarifas reales verificadas",
                email_evidence="Only explicit sender receipts; analysis is not evidence of delivery",
                limitations=["Public daily OHLC and previous daily FX, not executable bid/ask quotes",
                    "No real trades, taxes, cash dividends or broker settlement delays",
                    "D may exhaust its cash and cannot then represent the full A cohort"]))
    return experiment_id


def resume_model_activation(journal, experiment_id: str):
    """Repair headers after a crash, using their original clocks/settings/SHA."""
    store = create_model_results_store(journal)
    known = [row for row in store.list_seasons() if row["manifest"].get("experiment_id") == experiment_id]
    if not known:
        raise ValueError("No activation headers found for this owner")
    base = known[0]["manifest"]
    if base["engine_identity"]["signature"] != engine_identity()["signature"]:
        raise ModelResultsConflict("Engine changed; partial activation cannot be resumed with different rules")
    for mode in ("theoretical", "budget"):
        for model, spec in FROZEN_MODELS.items():
            manifest = {**base, "mode": mode, "strategy_key": model, "frozen_spec": asdict(spec),
                "initial_nav_usd": base["budget_usd"] if mode == "budget" else 0.0,
                "initial_cash_usd": base["budget_usd"] if mode == "budget" else 0.0,
                "funding_rule": "initial_fixed" if mode == "budget" else "external_100_per_entry"}
            store.create_season(manifest)
    return experiment_id


def _seasons(store, experiment_id):
    rows = [row for row in store.list_seasons() if row["manifest"].get("experiment_id") == experiment_id]
    by_key = {(row["manifest"]["mode"], row["manifest"]["strategy_key"]): row for row in rows}
    if len(by_key) != 8:
        raise ValueError("An experiment requires all eight immutable model/mode headers")
    return by_key


def _bar_rows(bars: dict, when: pd.Timestamp):
    for ticker, frame in sorted(bars.items()):
        for day, row in frame.iterrows():
            close = pd.Timestamp(row["close_at"])
            # Never archive an incomplete current session as its final close.
            if close > when:
                continue
            if "close_available_at" in row:
                available = pd.to_datetime(row["close_available_at"], utc=True, errors="coerce")
                if pd.isna(available) or available > when:
                    continue
            data = _safe(row.to_dict())
            if any(data.get(name) is None or data[name] <= 0 for name in ("open", "high", "low", "close")):
                continue
            data["session"] = pd.Timestamp(day).date().isoformat()
            data["ticker"] = ticker
            data["close_available_at"] = when.isoformat()
            data["source_close_available_at"] = when.isoformat()
            data["currency"] = frame.attrs.get("display_currency", "")
            yield ticker, data


def _same_bar(a, b):
    # Feed clocks/download timestamps change between observations, prices must
    # not. Provider revisions are incidents, never retroactive replacements.
    fields = ("open", "high", "low", "close", "fx_open", "fx_close",
              "sell_setup", "reduce_setup", "sma_long", "currency")
    return all(a.get(field) == b.get(field) for field in fields)


def _reconstruct_feed(events):
    observations, frames, attrs, proposals = [], {}, {}, {}
    for event in events:
        payload = event["payload"]
        if payload.get("feed_kind") == "signal":
            observations.append(payload["row"])
        elif payload.get("feed_kind") == "bar":
            row = dict(payload["row"])
            frames.setdefault(row.pop("ticker"), []).append(row)
        elif payload.get("feed_kind") == "calendar":
            attrs[payload["ticker"]] = payload["attrs"]
        elif event["event_type"] == "order_proposed":
            proposals[event["event_id"]] = event
    bars = {}
    for ticker, records in frames.items():
        frame = pd.DataFrame(records).set_index("session").sort_index()
        frame.index = pd.to_datetime(frame.index)
        for col in ("open_at", "close_at", "close_available_at", "source_close_available_at"):
            if col in frame:
                frame[col] = pd.to_datetime(frame[col], utc=True)
        frame.attrs.update(attrs.get(ticker, {}))
        frame.attrs["display_currency"] = records[0].get("currency", "")
        bars[ticker] = frame
    signals = pd.DataFrame(observations)
    if not signals.empty:
        signals["observed_at"] = pd.to_datetime(signals.observed_at, utc=True)
    return signals, bars, proposals


def collect_model_market_bundle(tickers, *, start, now=None):
    """Public download only; no signals, mail, portfolio writes or orders."""
    from src.strategy_audit_service import fetch_audit_market_bundle, exchange_schedule
    when = _clock(now)
    bundle = fetch_audit_market_bundle(tickers, start, when.date().isoformat())
    # Future opens are known calendar facts, not future prices. Needed to record
    # orders before an executable session rather than fabricate them afterwards.
    for ticker, frame in bundle.bars.items():
        info = bundle.metadata[ticker]
        schedule = exchange_schedule(info, (pd.Timestamp(start) - pd.Timedelta(days=450)).date().isoformat(),
            (when + pd.Timedelta(days=12)).date().isoformat())
        frame.attrs["session_schedule"] = [dict(session=pd.Timestamp(day).date().isoformat(),
            open_at=row.market_open.isoformat(), close_at=row.market_close.isoformat()) for day, row in schedule.iterrows()]
        completed = schedule.loc[schedule.market_close <= when]
        frame.attrs["expected_sessions"] = [pd.Timestamp(day).date().isoformat() for day in completed.index]
        frame.attrs["required_last_close_at"] = completed.market_close.iloc[-1].isoformat() if not completed.empty else None
        frame.attrs["calendar_coverage_start"] = schedule.attrs.get("calendar_coverage_start", str(schedule.index[0].date()))
        frame.attrs["calendar_coverage_end"] = (when + pd.Timedelta(days=12)).date().isoformat()
    return bundle


def advance_model_experiment(journal, experiment_id: str, snapshots, bars: dict, *, now=None) -> dict:
    """Replay only archived first-seen inputs; append new intentions and facts.

    Call after signal generation and after completed sessions. Repeated identical
    inputs do not append anything. Late observations get a late knowledge clock.
    A fill without an already archived pre-open intention fails closed.
    """
    when = _clock(now)
    store = create_model_results_store(journal)
    seasons = _seasons(store, experiment_id)
    feed_id = seasons[("theoretical", "A")]["season_id"]
    manifest = seasons[("theoretical", "A")]["manifest"]
    if engine_identity()["signature"] != manifest["engine_identity"]["signature"]:
        raise ModelResultsConflict("Frozen engine changed: start a separately versioned experiment, never rewrite this one")
    activation = _clock(manifest["activation_at"])
    if when < activation:
        raise ValueError("The collector cannot run before activation")
    prior = store.list_events(feed_id)
    known = {row["event_id"]: row for row in prior}
    feed, incidents = [], []
    signal_set = normalize_audit_signals(snapshots, source=manifest["source"])
    normalized = signal_set.signals
    first_collection = not any(row["payload"].get("feed_kind") == "signal" for row in prior)
    if not normalized.empty:
        baseline = normalized.loc[normalized.observed_at < activation].sort_values("observed_at").groupby("ticker").tail(1)
        eligible = normalized.loc[normalized.observed_at >= activation]
        if first_collection:
            eligible = pd.concat([baseline, eligible])
        for row in eligible.to_dict("records"):
            native_clock = _clock(row["observed_at"])
            if native_clock > when:
                continue
            # Baseline only establishes prior activation state. It cannot buy.
            effective = native_clock if native_clock < activation else max(native_clock, when)
            source_id = row["event_id"]
            key = deterministic_event_id("observation", ["signal", source_id])
            if key in known:
                continue
            data = _safe({**row, "source_observed_at": native_clock.isoformat(),
                          "observed_at": effective.isoformat(), "model_seen_at": when.isoformat()})
            feed.append(_event("observation", ["signal", source_id], native_clock,
                {"feed_kind": "signal", "row": data}, available=when))
    for ticker, data in _bar_rows(bars, when):
        key = deterministic_event_id("price_mark", [ticker, data["session"]])
        if key in known:
            if not _same_bar(known[key]["payload"]["row"], data):
                # Stable revision ID; repeating a known correction is a no-op.
                incidents.append(_event("incident", ["provider_revision", ticker, data["session"],
                    content_hash({k: data.get(k) for k in ("open", "close", "fx_close", "currency")})], when,
                    {"code": "provider_revision_not_applied", "ticker": ticker, "session": data["session"],
                     "original_quote_hash": content_hash(known[key]["payload"]["row"]),
                     "revision_quote_hash": content_hash(data), "conclusion_reliable": False}))
            continue
        feed.append(_event("price_mark", [ticker, data["session"]], data["close_at"],
            {"feed_kind": "bar", "row": data}, available=when))
    for ticker, frame in sorted(bars.items()):
        attrs = _safe({name: frame.attrs.get(name) for name in (
            "session_schedule", "expected_sessions", "required_last_close_at", "display_currency",
            "quarantined_from", "audit_data_version", "source_provider", "adjustment_basis",
            "calendar_coverage_start", "calendar_coverage_end", "provider",
            "fx_staleness_policy", "close_availability_basis")})
        # Same calendar facts must not be written once per retry timestamp.
        calendar = _event("observation", ["calendar", ticker, content_hash(attrs)], when,
            {"feed_kind": "calendar", "ticker": ticker, "attrs": attrs})
        if calendar["event_id"] not in known:
            feed.append(calendar)
    fresh = [event for event in feed + incidents if event["event_id"] not in known]
    if fresh:
        _append(store, feed_id, run_id="feed-" + content_hash([row["event_id"] for row in fresh]),
                now=when, events=fresh)
    feed_events = store.list_events(feed_id)
    frozen_signals, frozen_bars, _ = _reconstruct_feed(feed_events)
    if frozen_signals.empty:
        return {"experiment_id": experiment_id, "new_inputs": len(fresh), "runs": 0,
                "status": "waiting_for_signals"}
    input_hash = content_hash([row["event_hash"] for row in feed_events
        if row["payload"].get("feed_kind") in {"signal", "bar", "calendar"}
        or row["event_type"] == "incident"])
    results, run_count = {}, 0
    has_revisions = any(row["payload"].get("code") == "provider_revision_not_applied" for row in feed_events)
    for mode in ("theoretical", "budget"):
        reports = run_frozen_models(frozen_signals, frozen_bars, start=activation, end=when,
            mode=mode, budget_usd=manifest["budget_usd"] if mode == "budget" else None,
            costs=ExecutionCosts(**manifest["execution_costs"]) if mode == "budget" else ExecutionCosts(0, 0, 0),
            git_sha=git_revision())
        for model, report in reports.items():
            season_id = seasons[(mode, model)]["season_id"]
            existing = store.list_events(season_id)
            archived = {event["event_id"]: event for event in existing}
            events = []
            # Plan entries from the very same first-observation rule as replay.
            previous, seen = {}, set()
            for row in frozen_signals.sort_values(["observed_at", "event_id"]).to_dict("records"):
                ticker, source_id = row["ticker"], row["event_id"]
                session_key = (ticker, row["signal_session"])
                if session_key in seen:
                    continue
                seen.add(session_key)
                strong = row["entry_label"] == "Entrada fuerte"
                was = previous.get(ticker)
                previous[ticker] = strong
                at = _clock(row["observed_at"])
                qualifies = strong and at >= activation
                if model in {"B", "C"}:
                    qualifies = qualifies and was is False
                if model == "C":
                    qualifies = qualifies and pd.notna(row.get("entry_score")) and float(row["entry_score"]) >= 85
                if not qualifies:
                    continue
                schedule = frozen_bars.get(ticker, pd.DataFrame()).attrs.get("session_schedule", [])
                opening = next((item["open_at"] for item in schedule if _clock(item["open_at"]) > at), None)
                intent_id = deterministic_event_id("order_proposed", ["buy", source_id])
                # An expired opening cannot be proposed after it has happened.
                # Existing intentions are replayed unchanged, not rebuilt now.
                if intent_id not in archived and opening is not None and _clock(opening) > when:
                    events.append(_event("order_proposed", ["buy", source_id], at,
                        {"side": "buy", "ticker": ticker, "source_event_id": source_id,
                         "notional_usd": 100.0, "intended_open_at": opening,
                         "budget_checked_at_execution": mode == "budget"}, available=when))
                    events.append(_event("decision", ["buy", source_id], at,
                        {"action": "propose_buy", "ticker": ticker, "source_event_id": source_id,
                         "strategy": model, "input_hash": input_hash}, available=when))
            for item in report.manifest.get("pending_exit_orders", []):
                at = _clock(item["decision_at"])
                intent_id = deterministic_event_id("order_proposed", ["sell", item["lot_id"]])
                opening = item.get("intended_open_at")
                if intent_id not in archived and opening and _clock(opening) > when:
                    events.append(_event("order_proposed", ["sell", item["lot_id"]], at,
                        {**item, "side": "sell", "strategy": model}, available=when))
            missing_intentions = []
            fills = [("buy", row["lot_id"], row["event_id"], row["entry_at"], row)
                     for row in report.lots.to_dict("records")]
            fills += [("sell", row["lot_id"], row["event_id"], row["exit_at"], row)
                      for row in report.trades.to_dict("records")]
            for side, lot_id, source_id, executed, row in fills:
                key = source_id if side == "buy" else lot_id
                intent = archived.get(deterministic_event_id("order_proposed", [side, key]))
                if intent is None or _clock(intent["available_at"]) >= _clock(executed):
                    missing_intentions.append(lot_id)
                    continue
                target = intent["payload"].get("intended_open_at")
                if target is None or _clock(target) != _clock(executed):
                    missing_intentions.append(lot_id)
                    continue
                # Record the actual model trade fields, excluding terminal lot
                # state which changes when later prices/exit facts arrive.
                payload = {"side": side, "lot_id": lot_id, "source_event_id": source_id,
                    "ticker": row["ticker"], "quantity": row["quantity"],
                    "price_native": row["entry_price_native" if side == "buy" else "exit_price_native"],
                    "fx": row["entry_fx" if side == "buy" else "exit_fx"],
                    "gross_usd": row["contribution_usd" if side == "buy" else "proceeds_usd"],
                    "costs_usd": row["entry_costs_usd" if side == "buy" else "exit_costs_usd"],
                    "intention_id": intent["event_id"], "is_real_execution": False}
                fill_id = deterministic_event_id("order_filled", [side, lot_id])
                if fill_id not in archived:
                    events.append(_event("order_filled", [side, lot_id], executed, payload, available=when))
                elif archived[fill_id]["payload"] != _safe(payload):
                    raise ModelResultsConflict("Replayed trade differs from the immutable executed order")
            run_id = when.date().isoformat() + "-" + input_hash
            old_snapshots = store.list_snapshots(season_id)
            if any(row["run_id"] == run_id for row in old_snapshots):
                continue
            # Never show replay-only, unrecorded hypothetical fills as verified
            # prospective performance. Preserve diagnostics for operator review.
            already_blocked = any(row["payload"].get("code") == "prospective_execution_blocked" for row in existing)
            trusted = not missing_intentions and not has_revisions and not already_blocked
            if not trusted:
                # A replay-only sale must NEVER fund another recorded purchase.
                # Suppressing the displayed NAV is insufficient: freeze every
                # new economic action until an independent season is started.
                events = [event for event in events if event["event_type"] not in {
                    "order_filled", "order_proposed", "decision"}]
                if not already_blocked:
                    events.append(_event("incident", "prospective_execution_blocked", when,
                        {"code": "prospective_execution_blocked", "requires_new_season": True,
                         "missing_preopen_intentions": sorted(set(missing_intentions)),
                         "provider_revision_detected": has_revisions}))
            snapshot = dict(available_at=when.isoformat(), engine_version=TRACKING_VERSION,
                input_hash=input_hash, git_sha=git_revision(), source=manifest["source"],
                runtime_versions=runtime_versions(),
                runtime_differs_from_activation=runtime_versions() != manifest["engine_identity"].get("runtime_versions"),
                metrics=_safe(report.metrics) if trusted else None,
                replay_diagnostics=_safe(report.metrics) if not trusted else None,
                lots=_safe(report.lots.to_dict("records")) if trusted else [],
                trades=_safe(report.trades.to_dict("records")) if trusted else [],
                # Prior daily snapshots already preserve their own facts;
                # avoid copying the full path on every collection (quadratic
                # storage growth on the free database tier).
                daily=_safe(report.daily.tail(1).reset_index().to_dict("records")) if trusted else [],
                coverage_complete=bool(trusted and report.metrics["coverage_complete"]),
                missing_preopen_intentions=sorted(set(missing_intentions)),
                provider_revision_detected=has_revisions, feed_season_id=feed_id,
                limitations=["Daily OHLC; hypothesized opens, not broker fills", "SMTP acceptance does not prove inbox delivery"])
            from src.model_results_service import _benchmark, _matched_hold
            from src.strategy_audit_service import BENCHMARKS
            snapshot["benchmarks"] = [dict(name=name, ticker=symbol, **_safe(_benchmark(report,
                frozen_bars.get(symbol, pd.DataFrame()), mode=mode, start=activation, end=when,
                budget_usd=manifest["budget_usd"], costs=ExecutionCosts(**manifest["execution_costs"]))))
                for name, symbol in BENCHMARKS.items()] if trusted else []
            if trusted and model in {"A", "B", "C"}:
                hold = _matched_hold(frozen_signals, frozen_bars, report, mode=mode,
                    start=activation, end=when, budget_usd=manifest["budget_usd"],
                    costs=ExecutionCosts(**manifest["execution_costs"]), git_sha=git_revision())
                snapshot["matched_hold"] = {"metrics": _safe(hold.metrics), "manifest": _safe(hold.manifest)}
            _append(store, season_id, run_id=run_id, now=when,
                events=[event for event in events if event["event_id"] not in archived], snapshot=snapshot)
            results[mode + "/" + model] = snapshot["coverage_complete"]
            run_count += 1
    return {"experiment_id": experiment_id, "new_inputs": len(fresh), "runs": run_count,
            "coverage": results, "status": "updated" if run_count else "unchanged"}


def record_model_email_evidence(journal, receipt, delivery_id, *, evaluated_at=None):
    """Optional receipt mirror. No send, preferences, recipients or credentials.

    Gate is off by default, and must be enabled alongside scheduled tracking.
    Sender stubs returning None are never upgraded into confirmed SMTP events.
    """
    if os.getenv("MODEL_RESULTS_ENABLED", "0").lower() not in {"1", "true"}:
        return
    store = create_model_results_store(journal)
    seasons = [row for row in store.list_seasons() if row["manifest"].get("engine_version") == TRACKING_VERSION]
    if not seasons:
        return
    when = _clock()
    status = receipt.get("status") if isinstance(receipt, dict) else getattr(receipt, "status", None)
    payload = {"delivery_id": str(delivery_id), "status": status if status in {"smtp_accepted", "failed"} else "attempted"}
    if status == "smtp_accepted":
        payload.update(smtp_code=getattr(receipt, "smtp_code", None),
            receipt_hash=getattr(receipt, "receipt_hash", None), accepted_at=getattr(receipt, "accepted_at", None),
            message_hash=getattr(receipt, "message_hash", None))
    if evaluated_at is not None:
        payload["signal_evaluated_at"] = str(evaluated_at)
    event = _event("email_delivery", [str(delivery_id), payload["status"]], when, payload)
    # One shared feed per experiment; never copy one email into eight strategies.
    for season in seasons:
        info = season["manifest"]
        if (info["mode"], info["strategy_key"]) != ("theoretical", "A"):
            continue
        existing = store.list_events(season["season_id"])
        if any(row["event_id"] == event["event_id"] for row in existing):
            continue
        _append(store, season["season_id"], run_id="mail-" + event["event_id"], now=when, events=[event])
