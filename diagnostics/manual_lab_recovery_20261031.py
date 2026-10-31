"""Explicit one-off recovery: reuse the stored analysis, refresh only virtual tracking."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import io
import json
import logging
import os

from src.alert_runner import _virtual_report_summary
from src.alerts import AlertPreferences, is_valid_email
from src.daily_digest import _sessions, daily_score_coverage, render_daily_score_email, seal_daily_score_report
from src.email_sender import EmailDeliveryReceipt, send_email
from src.model_results_store import create_model_results_store, deterministic_event_id
from src.model_results_tracking import experiment_readiness, _event, _append
from src.storage import create_journal, load_supabase_config
from scripts.run_model_tracking import collect_owner_models


OWNER = "ddriu"
REQUEST = "explicit-lab-recovery-parser-v2-20261031"
REQUEST_DAY = date(2026, 10, 31)
SESSION = date(2026, 10, 30)
SOURCE_REPORT_ID = "28c0c27e51c2c9f81a516058afc78db0295b847e6d4ddace6faacc2e3f0e000a"
SUCCESS_STATUSES = frozenset({"updated", "unchanged", "waiting_for_signals"})


def recovery_key():
    return hashlib.sha256(json.dumps([REQUEST, OWNER, SESSION.isoformat()], separators=(",", ":")).encode()).hexdigest()


def validate_clock(clock):
    if clock.date() != REQUEST_DAY or os.getenv("GITHUB_RUN_ATTEMPT") != "1":
        raise RuntimeError("ExpiredOrRepeatedRecoveryRequest")
    if os.getenv("MODEL_RESULTS_ENABLED", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("VirtualTrackingMustBeEnabled")
    if os.getenv("REVIEW_SESSION", SESSION.isoformat()) != SESSION.isoformat():
        raise RuntimeError("UnexpectedRecoverySession")
    completed, _ = _sessions("SPY", clock.date().isoformat(), clock)
    if not completed or completed[0] != SESSION.isoformat():
        raise RuntimeError("SourceSessionIsNotLastCompletedClose")


def selected_experiment(journal):
    if getattr(journal, "owner", None) != OWNER: raise RuntimeError("UnexpectedLedgerOwner")
    grouped = {}
    for header in create_model_results_store(journal).list_seasons():
        identifier = (header.get("manifest") or {}).get("experiment_id")
        if identifier: grouped.setdefault(identifier, []).append(header)
    compatible = []
    for identifier, group in grouped.items():
        manifest = group[0].get("manifest") or {}
        if (manifest.get("source") == "growth" and manifest.get("cohort") == "verified"
                and experiment_readiness(group).get("ready") is True):
            if len(group) != 8: raise RuntimeError("IncompleteCompatibleActivation")
            compatible.append(identifier)
    if len(compatible) != 1: raise RuntimeError("ExpectedOneCompatibleGrowthExperiment")
    return compatible[0]


def validate_virtual(value, experiment):
    if not isinstance(value, dict) or value.get("status") not in SUCCESS_STATUSES:
        raise RuntimeError("VirtualCollectionNotConfirmed")
    if value.get("issues") != 0 or value.get("ledger_incidents") != 0 or value.get("ready_experiments") != 1:
        raise RuntimeError("VirtualCollectionHasIncidents")
    entries = value.get("experiments")
    if not isinstance(entries, list) or len(entries) != 1: raise RuntimeError("UnexpectedVirtualExperimentCount")
    item = entries[0]
    if (not isinstance(item, dict) or item.get("experiment_id") != experiment
            or item.get("source") != "growth" or item.get("cohort") != "verified"
            or item.get("strategy_accounts") != 8 or item.get("status") not in SUCCESS_STATUSES):
        raise RuntimeError("UnexpectedVirtualExperimentScope")
    for key in ("new_proposals", "new_fills", "pending_proposals"):
        count = value.get(key)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise RuntimeError("VirtualOperationCountsUnconfirmed")


def validate_source(report):
    if not isinstance(report, dict) or report.get("report_id") != SOURCE_REPORT_ID:
        raise RuntimeError("UnexpectedSourceReport")
    if seal_daily_score_report(report)["report_id"] != SOURCE_REPORT_ID:
        raise RuntimeError("SourceReportSealMismatch")
    if report.get("as_of") != SESSION.isoformat() or report.get("version") != "daily-score-report-v2":
        raise RuntimeError("UnexpectedSourceSessionOrVersion")
    if not isinstance(report.get("rows"), list) or not report["rows"]:
        raise RuntimeError("SourceAnalysisUnavailable")
    if (report.get("virtual_tracking") or {}).get("status") not in {"error", "partial_failure"}:
        raise RuntimeError("SourceIsNotTheApprovedFailedVirtualReview")


def mirror_receipt(journal, experiment, receipt, key):
    """Only the new experiment's shared feed, never the preserved ledgers."""
    store = create_model_results_store(journal)
    feeds = [row for row in store.list_seasons() if
        (row.get("manifest") or {}).get("experiment_id") == experiment
        and (row.get("manifest") or {}).get("mode") == "theoretical"
        and (row.get("manifest") or {}).get("strategy_key") == "A"]
    if len(feeds) != 1: raise RuntimeError("UnexpectedReceiptFeed")
    sid = feeds[0]["season_id"]
    identifier = deterministic_event_id("email_delivery", [key, "smtp_accepted"])
    if any(item["event_id"] == identifier for item in store.list_events(sid)): return
    now = datetime.now(timezone.utc)
    event = _event("email_delivery", [key, "smtp_accepted"], now,
        {"delivery_id": key, **asdict(receipt)})
    _append(store, sid, run_id="mail-" + identifier, now=now, events=[event])


def _recover():
    if load_supabase_config() is None: raise RuntimeError("MissingPrivateBackend")
    journal = create_journal(OWNER)
    if getattr(journal, "owner", None) != OWNER: raise RuntimeError("UnexpectedOwner")
    pref = journal.get_alert_preferences()
    if not isinstance(pref, AlertPreferences) or pref.owner != OWNER or pref.enabled is not True:
        raise RuntimeError("InvalidOwnerPreferences")
    if (not isinstance(pref.email, str) or pref.email != pref.email.strip()
            or any(character in pref.email for character in "\r\n") or not is_valid_email(pref.email)):
        raise RuntimeError("InvalidRecipient")
    source = journal.get_daily_score_report(SOURCE_REPORT_ID)
    validate_source(source)
    source_before = json.dumps(source, sort_keys=True, ensure_ascii=False, allow_nan=False)
    experiment = selected_experiment(journal)
    virtual = collect_owner_models(journal, snapshots=journal.list_analysis_snapshots(), experiment_id=experiment)
    validate_virtual(virtual, experiment)
    report = json.loads(source_before)
    report["virtual_tracking"] = _virtual_report_summary(virtual)
    validate_virtual(report["virtual_tracking"], experiment)
    report = seal_daily_score_report(report)
    if {k: v for k, v in report.items() if k not in {"report_id", "virtual_tracking"}} != {
            k: v for k, v in source.items() if k not in {"report_id", "virtual_tracking"}}:
        raise RuntimeError("StoredAnalysisChanged")
    if source_before != json.dumps(source, sort_keys=True, ensure_ascii=False, allow_nan=False):
        raise RuntimeError("SourceReportMutated")
    if journal.save_daily_score_report(report) != report["report_id"]:
        raise RuntimeError("UnexpectedStoredReportIdentity")
    key = recovery_key()
    if not journal.claim_daily_digest(key, SESSION.isoformat(), report["report_id"]):
        return {"status": "already_reserved_or_completed", "emails_sent": 0, "receipts": []}
    rendered = render_daily_score_email(report,
        app_url=f"https://stock-signal-lab-ddriu.streamlit.app/?digest={report['report_id']}")
    receipts, transport_error = [], None
    try:
        receipt = send_email(pref.email, *rendered)
    except Exception as exc:
        transport_error = type(exc).__name__
        receipt = getattr(exc, "smtp_receipt", None)
    accepted = isinstance(receipt, EmailDeliveryReceipt) and receipt.status == "smtp_accepted"
    if accepted: receipts.append(receipt)
    terminal_saved = True
    try:
        journal.finish_daily_digest(key, "sent" if accepted else "uncertain", error_type=transport_error or "")
    except Exception:
        terminal_saved = False
    mirrored = False
    if accepted:
        try:
            mirror_receipt(journal, experiment, receipt, key)
            mirrored = True
        except Exception:
            pass  # SMTP accepted: a mirror failure is never permission to retry.
    rows = report["rows"]
    return {"status": "smtp_accepted" if accepted else "delivery_unconfirmed",
        "emails_sent": int(accepted), "session": SESSION.isoformat(), "report_rows": len(rows),
        "held_rows": sum(bool(row.get("held")) for row in rows), "coverage": daily_score_coverage(rows),
        "favorites_coverage": daily_score_coverage(row for row in rows if not row.get("held")),
        "virtual_status": virtual["status"], "virtual_new_proposals": virtual["new_proposals"],
        "virtual_new_fills": virtual["new_fills"], "virtual_pending_proposals": virtual["pending_proposals"],
        "receipts": [asdict(item) for item in receipts], "terminal_saved": terminal_saved,
        "receipt_mirrored_to_selected_feed": mirrored,
        "scheduled_flag": os.getenv("SCHEDULED_MODEL_SWITCH", "unknown"),
        "scheduled_flag_basis": "workflow declaration; not inferred from this one-off run"}


def execute():
    validate_clock(datetime.now(timezone.utc))
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = _recover()
    finally:
        logging.disable(previous)
    print(json.dumps(result, sort_keys=True))
    if result["status"] not in {"smtp_accepted", "already_reserved_or_completed"}:
        raise RuntimeError("RecoveryDeliveryUnconfirmed")
    return result


if __name__ == "__main__":
    try:
        execute()
    except Exception as exc:
        print(json.dumps({"status": "recovery_requires_attention", "error_type": type(exc).__name__}))
        raise SystemExit(1)
