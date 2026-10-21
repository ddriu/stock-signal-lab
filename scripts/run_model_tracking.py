"""Scheduled virtual tracking only: no SMTP and no real-trade credentials.

Activation is explicit in the authenticated Results page. The scheduler only
advances existing seasons; the workflow's MODEL_RESULTS_ENABLED repository
variable is a separate deployment switch. No account data is printed/exported.
"""
from __future__ import annotations

import os
import sys

from src.model_results_store import create_model_results_store
from src.model_results_tracking import (TRACKING_VERSION, advance_model_experiment,
    collect_model_market_bundle)
from src.storage import create_journal, GROUP_PORTFOLIO_OWNER
from src.strategy_audit_service import BENCHMARKS, audit_universe


def enabled():
    return os.getenv("MODEL_RESULTS_ENABLED", "0").lower() in {"1", "true"}


def advance_one(journal, store, seasons, snapshots, experiment, manifest):
    headers = [row for row in seasons if row["manifest"].get("experiment_id") == experiment]
    if len({(row["manifest"]["mode"], row["manifest"]["strategy_key"]) for row in headers}) != 8:
        raise ValueError("Incomplete activation: resume the same activation from the private panel")
    tickers = set(audit_universe(snapshots)) | set(BENCHMARKS.values())
    feed = next(row for row in headers if row["manifest"]["strategy_key"] == "A"
        and row["manifest"]["mode"] == "theoretical")
    for event in store.list_events(feed["season_id"]):
        ticker = event["payload"].get("row", {}).get("ticker")
        if ticker:
            tickers.add(ticker)
    bundle = collect_model_market_bundle(sorted(tickers), start=manifest["start_date"])
    return advance_model_experiment(journal, experiment, snapshots, bundle.bars)


def main() -> int:
    if not enabled():
        print("Seguimiento virtual desactivado por configuración; no hubo escrituras ni correos.")
        return 0
    # The backend service key already owns this table. Discover only activated
    # owners server-side, without publishing a private owner allow-list in Git.
    backend = create_journal(GROUP_PORTFOLIO_OWNER)
    if not getattr(backend, "url", None):
        print("El proceso programado requiere almacenamiento remoto configurado.", file=sys.stderr)
        return 1
    records = backend._list_rows(endpoint=backend.url + "/rest/v1/model_result_seasons",
        params={"select": "owner,season_id", "namespace": "eq.model-original-v1", "order": "owner.asc,season_id.asc"})
    owners = sorted({row["owner"] for row in records})
    count, failures = 0, 0
    for owner in owners:
        try:
            journal = create_journal(owner)
            store = create_model_results_store(journal)
            seasons = store.list_seasons()
            experiments = {row["manifest"]["experiment_id"]: row["manifest"] for row in seasons
                if row["manifest"].get("engine_version") == TRACKING_VERSION}
            snapshots = journal.list_analysis_snapshots()
            for experiment, manifest in experiments.items():
                try:
                    result = advance_one(journal, store, seasons, snapshots, experiment, manifest)
                    count += result["runs"]
                except Exception as error:
                    print(f"Temporada incompleta: {type(error).__name__}; las demás continúan.", file=sys.stderr)
                    failures += 1
        except Exception as error:
            # Exception strings may contain a URL, owner or private source data.
            print(f"Seguimiento incompleto: {type(error).__name__}; consulta el panel privado.", file=sys.stderr)
            failures += 1
    print(f"Seguimiento virtual: {count} resultados nuevos, {failures} incidencias de proceso. No se enviaron correos.")
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
