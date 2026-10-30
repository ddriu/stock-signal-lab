"""One explicitly requested review after the provenance repair; not a scheduler."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import io
import json
import logging
import os
import re

from src.alert_runner import run_daily_alerts
from src.alerts import AlertPreferences, is_valid_email
from src.daily_digest import _sessions, daily_score_coverage, render_daily_score_email, seal_daily_score_report
from src.email_sender import EmailDeliveryReceipt, send_email
from src.storage import GROUP_PORTFOLIO_OWNER, create_journal, load_supabase_config


OWNER = "ddriu"
REQUEST = "explicit-review-after-provenance-20261030-v1"
REQUEST_DAY = date(2026, 10, 30)
READ_METHODS = frozenset({"get_alert_preferences", "list_favorites", "open_positions",
    "list_portfolio_snapshot_positions", "list_alert_states", "list_analysis_snapshots"})


def manual_key(daily_key: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", daily_key):
        raise ValueError("InvalidDeliveryKey")
    return hashlib.sha256(json.dumps([REQUEST, daily_key], separators=(",", ":")).encode()).hexdigest()


def requested_session(clock: datetime) -> date:
    if clock.date() != REQUEST_DAY or os.getenv("GITHUB_RUN_ATTEMPT") != "1":
        raise RuntimeError("ExpiredOrRepeatedReviewRequest")
    if os.getenv("MODEL_RESULTS_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off"}:
        raise RuntimeError("UnexpectedModelTracking")
    raw = os.getenv("REVIEW_SESSION", "")
    try:
        session = date.fromisoformat(raw)
    except ValueError as exc:
        raise RuntimeError("InvalidReviewSession") from exc
    if session.isoformat() != raw:
        raise RuntimeError("InvalidReviewSession")
    completed, _ = _sessions("SPY", clock.date().isoformat(), clock)
    if not completed or session.isoformat() != completed[0]:
        raise RuntimeError("ReviewSessionIsNotLastCompletedClose")
    return session


class ReadOnlyGroup:
    def __init__(self, journal):
        self.journal = journal

    def __getattr__(self, name):
        if name not in READ_METHODS - {"get_alert_preferences", "list_alert_states", "list_analysis_snapshots"}:
            raise AttributeError("UnexpectedGroupAccess")
        return getattr(self.journal, name)


class ManualJournal:
    def __init__(self, journal, session: date):
        self.journal, self.session, self.report = journal, session, None

    def __getattr__(self, name):
        if name not in READ_METHODS | {"add_analysis_snapshot", "upsert_alert_states"}:
            raise AttributeError("UnexpectedJournalAccess")
        return getattr(self.journal, name)

    def save_daily_score_report(self, report):
        if report.get("as_of") != self.session.isoformat():
            raise ValueError("UnexpectedReportSession")
        if report.get("version") != "daily-score-report-v2":
            raise ValueError("UnexpectedReportVersion")
        if seal_daily_score_report(report)["report_id"] != report.get("report_id"):
            raise ValueError("UnsealedReport")
        self.report = json.loads(json.dumps(report))
        return self.journal.save_daily_score_report(report)

    def claim_daily_digest(self, key, market_date, report_id):
        if market_date != self.session.isoformat() or not self.report or self.report["report_id"] != report_id:
            raise ValueError("UnexpectedDeliveryScope")
        return self.journal.claim_daily_digest(manual_key(key), market_date, report_id)

    def finish_daily_digest(self, key, status, *, error_type=""):
        return self.journal.finish_daily_digest(manual_key(key), status, error_type=error_type)


def _review(session: date):
    if load_supabase_config() is None:
        raise RuntimeError("MissingPrivateBackend")
    journal = ManualJournal(create_journal(OWNER), session)
    pref = journal.get_alert_preferences()
    if not isinstance(pref, AlertPreferences) or pref.owner != OWNER or pref.enabled is not True:
        raise RuntimeError("InvalidOwnerPreferences")
    if (not isinstance(pref.email, str) or pref.email != pref.email.strip()
            or any(character in pref.email for character in "\r\n") or not is_valid_email(pref.email)):
        raise RuntimeError("InvalidRecipient")
    receipts, attempted = [], False

    def factory(owner):
        if owner == OWNER:
            return journal
        if owner == GROUP_PORTFOLIO_OWNER and pref.include_group:
            return ReadOnlyGroup(create_journal(owner))
        raise RuntimeError("UnexpectedOwnerAccess")

    def sender(recipient, subject, plain, html):
        nonlocal attempted
        if attempted or recipient != pref.email or journal.report is None:
            raise RuntimeError("UnexpectedSend")
        expected = render_daily_score_email(journal.report,
            app_url=f"https://stock-signal-lab-ddriu.streamlit.app/?digest={journal.report['report_id']}")
        if (subject, plain, html) != expected:
            raise RuntimeError("UnexpectedReportRendering")
        if seal_daily_score_report(journal.report)["report_id"] != journal.report["report_id"]:
            raise RuntimeError("RendererChangedReport")
        attempted = True
        try:
            receipt = send_email(recipient, subject, plain, html)
        except Exception as exc:
            receipt = getattr(exc, "smtp_receipt", None)
            if isinstance(receipt, EmailDeliveryReceipt) and receipt.status == "smtp_accepted":
                receipts.append(receipt)
            raise
        if isinstance(receipt, EmailDeliveryReceipt) and receipt.status == "smtp_accepted":
            receipts.append(receipt)
        return receipt

    result = run_daily_alerts(owner=OWNER, journal_factory=factory, sender=sender,
        today=session, require_current_session=True)
    return result, journal.report or {}, receipts


def execute():
    clock = datetime.now(timezone.utc)
    session = requested_session(clock)
    # Suppress provider and private-journal diagnostics in public Actions logs.
    previous_log_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result, report, receipts = _review(session)
    finally:
        logging.disable(previous_log_level)
    rows = report.get("rows") or []
    summary = {
        "status": "smtp_accepted" if receipts else "analysis_not_sent",
        "session": session.isoformat(), "users_checked": result.users_checked,
        "tickers_checked": result.tickers_checked, "tickers_with_prices": result.tickers_with_prices,
        "tickers_with_fresh_prices": result.tickers_with_fresh_prices,
        "report_rows": len(rows), "held_rows": sum(bool(row.get("held")) for row in rows),
        "coverage": daily_score_coverage(rows),
        "favorites_coverage": daily_score_coverage(row for row in rows if not row.get("held")),
        "emails_sent": result.emails_sent, "emails_skipped": result.emails_skipped,
        "incidents_count": len(result.errors), "receipts": [asdict(receipt) for receipt in receipts],
    }
    print(json.dumps(summary, sort_keys=True))
    if result.users_checked != 1 or result.emails_sent != 1 or len(receipts) != 1:
        raise RuntimeError("NoSingleVerifiedAnalysisEmail")


if __name__ == "__main__":
    try:
        execute()
    except Exception as exc:
        print(json.dumps({"status": "review_requires_attention", "error_type": type(exc).__name__}))
        raise SystemExit(1)
