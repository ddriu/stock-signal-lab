"""One explicitly requested private review; never import as a daily scheduler."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import io
import json
import logging
import re

from src.alert_runner import run_daily_alerts
from src.alerts import AlertPreferences, is_valid_email
from src.daily_digest import seal_daily_score_report
from src.email_sender import EmailDeliveryReceipt, send_email
from src.storage import GROUP_PORTFOLIO_OWNER, create_journal, load_supabase_config

from manual_full_review_renderer_20261029 import render_manual_review


OWNER = "ddriu"
SESSION = date(2026, 10, 28)
REQUEST = "explicit-full-review-20261029-v1"


def manual_key(daily_key: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", daily_key):
        raise ValueError("InvalidDeliveryKey")
    # A newly requested delivery, not a recycled/removed automatic reservation.
    return hashlib.sha256(json.dumps([REQUEST, daily_key], separators=(",", ":")).encode()).hexdigest()


class ManualJournal:
    def __init__(self, journal):
        self.journal = journal
        self.report = None

    def __getattr__(self, name):
        return getattr(self.journal, name)

    def save_daily_score_report(self, report):
        if report.get("as_of") != SESSION.isoformat():
            raise ValueError("UnexpectedReportSession")
        if seal_daily_score_report(report)["report_id"] != report.get("report_id"):
            raise ValueError("UnsealedReport")
        self.report = json.loads(json.dumps(report))
        return self.journal.save_daily_score_report(report)

    def claim_daily_digest(self, key, market_date, report_id):
        if market_date != SESSION.isoformat() or not self.report or self.report["report_id"] != report_id:
            raise ValueError("UnexpectedDeliveryScope")
        return self.journal.claim_daily_digest(manual_key(key), market_date, report_id)

    def finish_daily_digest(self, key, status, *, error_type=""):
        return self.journal.finish_daily_digest(manual_key(key), status, error_type=error_type)


def execute():
    if datetime.now(timezone.utc).date() != date(2026, 10, 29):
        raise RuntimeError("ExpiredReviewRequest")
    if load_supabase_config() is None:
        raise RuntimeError("MissingPrivateBackend")
    journal = ManualJournal(create_journal(OWNER))
    pref = journal.get_alert_preferences()
    if not isinstance(pref, AlertPreferences) or pref.owner != OWNER or pref.enabled is not True:
        raise RuntimeError("InvalidOwnerPreferences")
    if (not isinstance(pref.email, str) or pref.email != pref.email.strip()
            or any(c in pref.email for c in "\r\n") or not is_valid_email(pref.email)):
        raise RuntimeError("InvalidRecipient")
    receipts = []
    attempted = False

    def factory(owner):
        if owner == OWNER:
            return journal
        if owner == GROUP_PORTFOLIO_OWNER and pref.include_group:
            return create_journal(owner)
        raise RuntimeError("UnexpectedOwnerAccess")

    def sender(recipient, _subject, _plain, _html):
        nonlocal attempted
        if attempted or recipient != pref.email or journal.report is None:
            raise RuntimeError("UnexpectedSend")
        attempted = True
        generated = datetime.now(timezone.utc).isoformat()
        subject, plain, html = render_manual_review(journal.report, generated_at=generated)
        if seal_daily_score_report(journal.report)["report_id"] != journal.report["report_id"]:
            raise RuntimeError("RendererChangedReport")
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

    # Library diagnostics may contain private tickers/HTTP bodies. Keep them out
    # of public Actions logs; the private report contains user-facing incidents.
    previous_log_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = run_daily_alerts(owner=OWNER, journal_factory=factory, sender=sender,
                                      today=SESSION, require_current_session=True)
    finally:
        logging.disable(previous_log_level)
    report = journal.report or {}
    rows = report.get("rows") or []
    summary = {
        "status": "smtp_accepted" if receipts else "analysis_not_sent",
        "users_checked": result.users_checked,
        "tickers_checked": result.tickers_checked,
        "tickers_with_prices": result.tickers_with_prices,
        "tickers_with_fresh_prices": result.tickers_with_fresh_prices,
        "report_rows": len(rows),
        "held_rows": sum(bool(row.get("held")) for row in rows),
        "missing_scores": sum(row.get("score") is None for row in rows),
        "entry_strong": sum(not row.get("held") and row.get("status") == "entry_strong"
                            and (row.get("quality") or {}).get("block_entry") is False for row in rows),
        "entry_candidates": sum(not row.get("held") and row.get("status") == "entry_candidate"
                                and (row.get("quality") or {}).get("block_entry") is False for row in rows),
        "emails_sent": result.emails_sent,
        "emails_skipped": result.emails_skipped,
        "incidents_count": len(result.errors),
        "receipts": [asdict(receipt) for receipt in receipts],
    }
    # Counts and opaque SMTP hashes only, never names, email or report payload.
    print(json.dumps(summary, sort_keys=True))
    if result.users_checked != 1 or result.emails_sent != 1 or len(receipts) != 1:
        raise RuntimeError("NoSingleVerifiedAnalysisEmail")


if __name__ == "__main__":
    try:
        execute()
    except Exception as exc:
        print(json.dumps({"status": "review_requires_attention", "error_type": type(exc).__name__}))
        raise SystemExit(1)
