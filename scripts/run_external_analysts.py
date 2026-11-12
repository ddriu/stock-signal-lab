"""Explicit, opt-in third-party ingestion/tracking/mail; never broker trading."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from zoneinfo import ZoneInfo

from src.email_sender import load_email_config
from src.external_analyst_ingest import IMAPConfig
from src.external_analyst_service import (advance_comparison, build_external_report,
    deliver_external_report, first_observations, ingest_existing_mailbox)
from src.external_analyst_store import ExternalAnalystStore
from src.external_analysts import stable_id, utc_timestamp
from src.storage import create_journal, load_supabase_config


def enabled(name):
    return os.getenv(name, "false").strip().lower() in {"true", "1", "yes"}


def run_one(journal, *, now, receive=False, advance=False, send=False,
            email_config=None, market_loader=None, client_factory=None, smtp_send=None):
    store = ExternalAnalystStore(journal)
    config = email_config or load_email_config()
    outcome = {"mailbox": "not_requested", "tracking": "not_requested", "delivery": "not_requested"}
    def incident(stage, error):
        payload = {"kind": "runner_incident", "observed_at": utc_timestamp(now),
                   "stage": stage, "code": type(error).__name__}
        store.save_simulation_run(stable_id("runner-incident", payload), payload)
    if receive:
        try:
            inbox = IMAPConfig.from_existing_mail(config)
            result = ingest_existing_mailbox(store, now=now, config=inbox, client_factory=client_factory)
            outcome["mailbox"] = result["status"]
            outcome["extracted"] = result.get("imported", 0)
        except Exception as error:
            outcome["mailbox"] = "error"
            incident("mailbox", error)
    if advance:
        runs = store.list_simulation_runs()
        starts = [row["payload"] for row in runs if row["payload"].get("kind") == "activation"]
        if not starts:
            outcome["tracking"] = "not_activated"
        else:
            start = min(starts, key=lambda row: row["observed_at"])["observed_at"]
            if market_loader is None:
                from src.external_analyst_market import load_simulation_bars
                market_loader = load_simulation_bars
            candidates = first_observations(store, activation_at=start)
            candidates += [row["payload"]["candidate"] for row in runs
                           if row["payload"].get("kind") == "candidate"
                           and row["payload"]["observed_at"] >= start]
            try:
                market = market_loader(candidates, as_of=now, activation_at=start)
                advance_comparison(store, market["bars"], now=now,
                                   market_metadata={key: value for key, value in market.items() if key != "bars"})
                outcome["tracking"] = "recorded"
            except Exception as error:
                outcome["tracking"] = "error"
                incident("tracking", error)
    # Deliberately not the old alerts.enabled flag: external mail is opt-in
    # independently but reuses the same configured destination, unchanged.
    if send and store.get_preferences().get("enabled"):
        favorites = journal.list_favorites()
        favorites = favorites.to_dict("records") if hasattr(favorites, "to_dict") else favorites
        from src.portfolio_snapshot import latest_portfolio_snapshot, reconcile_current_portfolio
        import pandas as pd
        stored = journal.list_portfolio_snapshot_positions()
        positions = journal.open_positions()
        if not stored.empty:
            latest, _ = latest_portfolio_snapshot(stored)
            positions = reconcile_current_portfolio(latest, journal.list_operations(), positions)
        if "quantity" in positions:
            positions = positions.loc[pd.to_numeric(positions["quantity"], errors="coerce") > 0]
        positions = positions.to_dict("records")
        report = build_external_report(store, now=now,
            favorites={row.get("ticker") for row in favorites},
            held={row.get("analysis_ticker") or row.get("ticker") for row in positions})
        outcome["delivery"] = deliver_external_report(store, report, email_config=config, send=smtp_send)
    elif send:
        outcome["delivery"] = "disabled"
    return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receive", action="store_true")
    parser.add_argument("--advance", action="store_true")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--scheduled", action="store_true", help="Enviar solo entre 09:30–09:59 Madrid")
    args = parser.parse_args()
    if not enabled("EXTERNAL_ANALYSTS_ENABLED"):
        print("External analysts: disabled; no mailbox, SMTP or database changes.")
        return 0
    owner = os.getenv("EXTERNAL_ANALYSTS_MAILBOX_OWNER", "").strip()
    if not owner or owner == "grupo_compartido":
        raise SystemExit("Configure the explicitly authorized private mailbox owner.")
    if load_supabase_config() is None:
        raise SystemExit("Private Supabase storage is required for scheduled tracking.")
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo("Europe/Madrid"))
    send = args.send and enabled("EXTERNAL_ANALYSTS_SEND_ENABLED")
    if args.scheduled:
        send = send and local.weekday() < 5 and local.hour == 9 and local.minute >= 30
    outcome = run_one(create_journal(owner), now=now.isoformat(), receive=args.receive,
                      advance=args.advance, send=send)
    # Counts/status only; never print PII, letters, URLs with tokens or quotes.
    print("External analysts status:", outcome)
    return 1 if outcome.get("mailbox") == "error" or outcome.get("tracking") == "error" or outcome.get("delivery") == "uncertain" else 0


if __name__ == "__main__":
    raise SystemExit(main())
