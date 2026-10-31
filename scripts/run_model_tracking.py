"""Scheduled virtual tracking only: no SMTP and no real-trade credentials.

Activation is explicit in the authenticated Results page. The scheduler only
advances existing seasons; the workflow's MODEL_RESULTS_ENABLED repository
variable is a separate deployment switch. No account data is printed/exported.
"""
from __future__ import annotations

import os
import math
import re
import sys
from copy import deepcopy
from datetime import datetime, timezone

import pandas as pd

from src.model_results_store import create_model_results_store
from src.model_results_tracking import (TRACKING_VERSION, advance_model_experiment,
    collect_model_market_bundle, experiment_readiness, ModelTrackingBacklog)
from src.storage import create_journal, GROUP_PORTFOLIO_OWNER
from src.strategy_audit_service import BENCHMARKS, audit_universe


def enabled():
    return os.getenv("MODEL_RESULTS_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def _prospective_prices(bars, activation_at):
    """Keep precomputed indicators but do not archive 450 days of warmup.

    One final pre-activation close is retained as a price anchor. Full known
    calendars, especially future openings, remain untouched in frame attrs.
    This changes collection size, not strategy rules or their execution clock.
    """
    activation = pd.Timestamp(_utc(activation_at))
    output = {}
    for ticker, frame in bars.items():
        if frame.empty:
            output[ticker] = frame.copy(deep=True)
            output[ticker].attrs = deepcopy(frame.attrs)
            continue
        if "close_at" not in frame:
            raise ValueError("Prepared virtual prices require close_at")
        closes = pd.to_datetime(frame["close_at"], utc=True, errors="coerce")
        if closes.isna().any():
            raise ValueError("Prepared virtual prices have an invalid close clock")
        retain = closes >= activation
        prior = closes[closes < activation]
        if not prior.empty:
            # Use positional selection; duplicate session indexes must remain
            # available for the frozen audit's duplicate validation.
            anchor = prior.max()
            retain |= closes == anchor
        output[ticker] = frame.loc[retain].copy(deep=True)
        output[ticker].attrs = deepcopy(frame.attrs)
    return output


def advance_one(journal, store, seasons, snapshots, experiment, manifest, *, now=None):
    headers = [row for row in seasons if row["manifest"].get("experiment_id") == experiment]
    if len({(row["manifest"]["mode"], row["manifest"]["strategy_key"]) for row in headers}) != 8:
        raise ValueError("Incomplete activation: resume the same activation from the private panel")
    readiness = experiment_readiness(headers)
    if not readiness["ready"]:
        from src.model_results_store import ModelResultsConflict
        raise ModelResultsConflict("Frozen experiment is incompatible; create a separately versioned activation, never reset its ledger")
    tickers = set(audit_universe(snapshots)) | set(BENCHMARKS.values())
    feed = next(row for row in headers if row["manifest"]["strategy_key"] == "A"
        and row["manifest"]["mode"] == "theoretical")
    for event in store.list_events(feed["season_id"]):
        ticker = event["payload"].get("row", {}).get("ticker")
        if ticker:
            tickers.add(ticker)
    bundle = collect_model_market_bundle(sorted(tickers), start=manifest["start_date"])
    bars = _prospective_prices(bundle.bars, manifest["activation_at"])
    return advance_model_experiment(journal, experiment, snapshots, bars, now=now)


def _utc(value=None):
    when = value or datetime.now(timezone.utc)
    if not isinstance(when, datetime):
        when = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("Virtual collection requires a timezone-aware clock")
    return when.astimezone(timezone.utc)


def _operation_counts(store, headers, when):
    """Counts are per simulated strategy account, never broker operations."""
    proposals, fills, incidents, pending = set(), set(), set(), set()
    operation_events = {}
    for header in headers:
        season = header["season_id"]
        events = store.list_events(season)
        executed = {event["payload"].get("intention_id") for event in events
                    if event["event_type"] == "order_filled"}
        for event in events:
            key = (season, event["event_id"])
            if event["event_type"] == "order_filled":
                fills.add(key)
                operation_events[key] = (header["manifest"], event)
            elif event["event_type"] == "incident":
                incidents.add(key)
            elif event["event_type"] == "order_proposed":
                proposals.add(key)
                operation_events[key] = (header["manifest"], event)
                opening = event["payload"].get("intended_open_at")
                if opening and event["event_id"] not in executed and _utc(opening) > when:
                    pending.add(key)
    return dict(proposals=proposals, fills=fills, incidents=incidents, pending=pending,
                operation_events=operation_events)


def _operation_preview(manifest, event):
    """Only allow-listed simulated order fields, never raw ledger payloads."""
    payload = event["payload"]
    record = dict(event_type=event["event_type"], is_real_execution=False)
    for key, value, allowed in (
        ("ticker", payload.get("ticker", ""), None),
        ("side", payload.get("side"), {"buy", "sell"}),
        ("strategy", manifest.get("strategy_key"), set("ABCD")),
        ("mode", manifest.get("mode"), {"theoretical", "budget"}),
    ):
        if (allowed is not None and value in allowed) or (
                key == "ticker" and re.fullmatch(r"[A-Z0-9.^=\-]{1,32}", str(value))):
            record[key] = value
    if payload.get("intended_open_at"):
        record["intended_open_at"] = _utc(payload["intended_open_at"]).isoformat()
    if event["event_type"] == "order_filled" and event.get("occurred_at"):
        record["executed_at"] = _utc(event["occurred_at"]).isoformat()
    for key, value in (("notional_usd", payload.get("notional_usd", payload.get("gross_usd"))),
                       ("price_native", payload.get("price_native"))):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            record[key] = value
    return record


def collect_owner_models(journal, snapshots=None, *, now=None, experiment_id=None):
    """Explicit, owner-scoped virtual stage reusable before sealing a digest.

    The caller supplies its already persisted observations where available. No
    activation, SMTP, holdings mutation or global enabling happens here. The
    scheduled entrypoint owns its switch; a manually authorized caller may use
    this stage independently. Old incompatible ledgers are preserved and omitted.
    """
    when = _utc(now)
    summary = dict(status="no_activation", available_at=when.isoformat(),
        experiments=[], ready_experiments=0, preserved_experiments=0, runs=0,
        issues=0, ledger_incidents=0, new_proposals=0, new_fills=0,
        cumulative_proposals=0, cumulative_fills=0, pending_proposals=0,
        operations=[], operations_preview_truncated=False,
        operation_unit="simulated_strategy_account_orders", is_real_execution=False)
    store = create_model_results_store(journal)
    seasons = store.list_seasons()
    experiments = {row["manifest"]["experiment_id"]: row["manifest"] for row in seasons
        if row["manifest"].get("engine_version") == TRACKING_VERSION
        and row["manifest"].get("experiment_id")
        and (experiment_id is None or row["manifest"]["experiment_id"] == experiment_id)}
    if snapshots is None and experiments:
        snapshots = journal.list_analysis_snapshots()
    statuses = []
    unknown_counts = False
    for experiment, manifest in experiments.items():
        headers = [row for row in seasons if row["manifest"].get("experiment_id") == experiment]
        readiness = experiment_readiness(headers)
        item = dict(experiment_id=experiment, source=manifest.get("source", "technical"),
            cohort=manifest.get("cohort", "raw"), strategy_accounts=len(headers),
            budget_usd=manifest.get("budget_usd"), runs=0)
        summary["experiments"].append(item)
        if not readiness["ready"]:
            item["status"] = readiness["status"]
            item["requires_new_experiment"] = readiness["requires_new_experiment"]
            if readiness["requires_new_experiment"]:
                summary["preserved_experiments"] += 1
            else:
                summary["issues"] += 1
            continue
        summary["ready_experiments"] += 1
        before = None
        try:
            before = _operation_counts(store, headers, when)
            # In live runs the tracker takes its clock AFTER downloading. Do not
            # backdate newly fetched evidence to collection start. Explicit now
            # is only the caller's reproducible point-in-time test boundary.
            result = advance_one(journal, store, seasons, snapshots, experiment, manifest, now=now)
            item.update(status=result.get("status", "updated"), runs=result.get("runs", 0),
                new_inputs=result.get("new_inputs", 0))
            summary["runs"] += item["runs"]
            statuses.append(item["status"])
        except ModelTrackingBacklog:
            item["status"] = "collector_backlog"
            summary["issues"] += 1
        except Exception as error:
            # Never include exception text, URLs, owner or source record details.
            item.update(status="error", error_type=type(error).__name__)
            summary["issues"] += 1
        try:
            after = _operation_counts(store, headers, _utc(now))
            summary["cumulative_proposals"] += len(after["proposals"])
            summary["cumulative_fills"] += len(after["fills"])
            summary["pending_proposals"] += len(after["pending"])
            summary["ledger_incidents"] += len(after["incidents"])
            if before is None:
                unknown_counts = True
            else:
                item["new_proposals"] = len(after["proposals"] - before["proposals"])
                item["new_fills"] = len(after["fills"] - before["fills"])
                summary["new_proposals"] += item["new_proposals"]
                summary["new_fills"] += item["new_fills"]
                new_orders = (after["proposals"] | after["fills"]) - (before["proposals"] | before["fills"])
                for key in sorted(new_orders):
                    if len(summary["operations"]) < 5:
                        summary["operations"].append(_operation_preview(*after["operation_events"][key]))
                    else:
                        summary["operations_preview_truncated"] = True
        except Exception as error:
            item["counts_status"] = "unavailable"
            item["counts_error_type"] = type(error).__name__
            summary["issues"] += 1
            unknown_counts = True
    if unknown_counts:
        for key in ("new_proposals", "new_fills", "cumulative_proposals", "cumulative_fills",
                    "pending_proposals", "ledger_incidents"):
            summary[key] = None
    if summary["issues"]:
        summary["status"] = "partial_failure" if statuses else "error"
    elif summary["runs"]:
        summary["status"] = "updated"
    elif "waiting_for_signals" in statuses:
        summary["status"] = "waiting_for_signals"
    elif statuses:
        summary["status"] = "unchanged"
    elif summary["preserved_experiments"]:
        summary["status"] = "needs_new_activation"
    summary["available_at"] = _utc(now).isoformat()
    return summary


# Keep a descriptive name for the before-delivery orchestration hook.
run_tracking_for_journal = collect_owner_models


def main() -> int:
    if not enabled():
        print("::warning::Seguimiento virtual desactivado: MODEL_RESULTS_ENABLED no está habilitado. "
              "No se han medido resultados ni hubo escrituras o correos. Activar temporadas no habilita el programador.")
        return 0
    # The backend service key already owns this table. Discover only activated
    # owners server-side, without publishing a private owner allow-list in Git.
    backend = create_journal(GROUP_PORTFOLIO_OWNER)
    if not getattr(backend, "url", None):
        print("El proceso programado requiere almacenamiento remoto configurado.", file=sys.stderr)
        return 1
    try:
        requested_owner = os.getenv("MODEL_RESULTS_OWNER", "").strip()
        params = {"select": "owner,season_id", "namespace": "eq.model-original-v1", "order": "owner.asc,season_id.asc"}
        if requested_owner:
            params["owner"] = "eq." + requested_owner
        records = backend._list_rows(endpoint=backend.url + "/rest/v1/model_result_seasons", params=params)
    except Exception as error:
        print(f"::error::No se pudo consultar la activación virtual ({type(error).__name__}); no se enviaron correos.", file=sys.stderr)
        return 1
    owners = sorted({row["owner"] for row in records
                    if not requested_owner or row.get("owner") == requested_owner})
    count, failures, waiting, unchanged, backlogs = 0, 0, 0, 0, 0
    if not owners:
        print("::warning::Seguimiento habilitado sin experimentos activados; no hay resultados que actualizar ni rentabilidad medida.")
    for owner in owners:
        try:
            journal = create_journal(owner)
            result = collect_owner_models(journal)
            count += result["runs"]
            failures += result["issues"]
            waiting += sum(item.get("status") == "waiting_for_signals" for item in result["experiments"])
            unchanged += sum(item.get("status") == "unchanged" for item in result["experiments"])
            backlogs += sum(item.get("status") == "collector_backlog" for item in result["experiments"])
            if result["preserved_experiments"]:
                print("::warning::Se conservan experimentos históricos incompatibles sin modificarlos; "
                      "sólo se actualizan activaciones compatibles con su versión congelada.")
            if result["issues"]:
                print("::error::Seguimiento parcialmente actualizado; revisar incidencias/activación en el panel privado. "
                      "No se acumulan compras atrasadas ni se reinician registros.", file=sys.stderr)
        except Exception as error:
            # Exception strings may contain a URL, owner or private source data.
            print(f"Seguimiento incompleto: {type(error).__name__}; consulta el panel privado.", file=sys.stderr)
            failures += 1
    print(f"Seguimiento virtual: {count} resultados nuevos, {failures} incidencias de proceso, "
          f"{waiting} esperando señales, {unchanged} sin cambios y {backlogs} con recogidas atrasadas. No se enviaron correos.")
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
