"""One explicitly requested corrected closing review; no schedule or real trades."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from datetime import date, datetime, timezone
from html import escape
import hashlib
import io
import json
import logging
import os
import re
from zoneinfo import ZoneInfo

import pandas as pd

from src.alert_runner import run_daily_alerts
from src.alerts import AlertPreferences, is_valid_email
from src.daily_digest import _sessions, daily_score_coverage, render_daily_score_email, seal_daily_score_report
from src.email_sender import EmailDeliveryReceipt, send_email
from src.hourly_market_schedule import DAILY_CLOSE_PUBLICATION_MARGIN
from src.storage import GROUP_PORTFOLIO_OWNER, create_journal, load_supabase_config

OWNER = "ddriu"
SESSION = date(2026, 11, 4)
WINDOW_START = datetime.fromisoformat("2026-11-04T21:20:00+00:00")
WINDOW_END = datetime.fromisoformat("2026-11-05T11:00:00+00:00")
REQUEST = "manual-corrected-review-20261104-v1"


def validate_clock(clock=None):
    clock = clock or datetime.now(timezone.utc)
    if not isinstance(clock, datetime) or clock.tzinfo is None or clock.utcoffset() is None:
        raise RuntimeError("AwareClockRequired")
    if not WINDOW_START <= clock.astimezone(timezone.utc) < WINDOW_END:
        raise RuntimeError("ExpiredCorrectionRequest")
    if os.getenv("GITHUB_RUN_ATTEMPT") != "1":
        raise RuntimeError("RepeatedWorkflowAttempt")
    if os.getenv("MODEL_RESULTS_ENABLED", "false").lower() not in {"0", "false", "no", "off"}:
        raise RuntimeError("UnexpectedVirtualTracking")
    sessions, calendar = _sessions("SPY", clock.astimezone(ZoneInfo("Europe/Madrid")).date().isoformat(),
        pd.Timestamp(clock) - DAILY_CLOSE_PUBLICATION_MARGIN)
    if calendar != "XNYS" or not sessions or sessions[0] != SESSION.isoformat():
        raise RuntimeError("CorrectionTargetNeedsReview")
    return clock


def correction_key():
    # Intentionally independent of address, content or deployment: at most one
    # delivery for this request even if the address/report changes on a rerun.
    return hashlib.sha256(json.dumps([REQUEST, OWNER],
        separators=(",", ":")).encode()).hexdigest()


def preference(journal):
    if getattr(journal, "owner", None) != OWNER:
        raise RuntimeError("UnexpectedOwner")
    pref = journal.get_alert_preferences()
    if not isinstance(pref, AlertPreferences) or pref.owner != OWNER or pref.enabled is not True:
        raise RuntimeError("InvalidOwnerPreferences")
    if (not isinstance(pref.email, str) or not pref.email or pref.email != pref.email.strip()
            or any(c in pref.email for c in "\r\n") or not is_valid_email(pref.email)):
        raise RuntimeError("InvalidRecipient")
    return pref


def accepted(receipt):
    return (isinstance(receipt, EmailDeliveryReceipt) and receipt.status == "smtp_accepted"
            and receipt.smtp_code == 250)


class CorrectionJournal:
    """Only this owner's normal review writes plus a distinct delivery claim."""
    def __init__(self, journal, *, clock=None):
        preference(journal)
        self.journal, self.clock = journal, clock
        self.report = None

    def __getattr__(self, name):
        if name in {"add_operation", "delete_operation", "update_operation",
                    "save_paper_daily_run", "create_paper_simulation"}:
            raise RuntimeError("UnexpectedTradingWrite")
        return getattr(self.journal, name)

    def save_daily_score_report(self, report):
        validate_clock(self.clock)
        preference(self.journal)
        if (not isinstance(report, dict) or report.get("as_of") != SESSION.isoformat()
                or seal_daily_score_report(report)["report_id"] != report.get("report_id")
                or "virtual_tracking" in report):
            raise RuntimeError("UnexpectedCorrectionReport")
        if not daily_score_coverage(report.get("rows", []))["with_score"]:
            raise RuntimeError("NoObservedScoresForCorrection")
        self.report = json.loads(json.dumps(report, ensure_ascii=False))
        return self.journal.save_daily_score_report(report)

    def _check(self, key, market_date=None, report_id=None):
        if (not re.fullmatch(r"[0-9a-f]{64}", str(key)) or self.report is None
                or market_date is not None and market_date != SESSION.isoformat()
                or report_id is not None and report_id != self.report["report_id"]):
            raise RuntimeError("UnexpectedCorrectionClaim")

    def claim_daily_digest(self, key, market_date, report_id):
        self._check(key, market_date, report_id)
        validate_clock(self.clock)
        preference(self.journal)
        return self.journal.claim_daily_digest(correction_key(), market_date, report_id)

    def finish_daily_digest(self, key, status, *, error_type=""):
        self._check(key)
        return self.journal.finish_daily_digest(correction_key(), status, error_type=error_type)


class GroupReadOnly:
    def __init__(self, journal):
        self.journal = journal

    def __getattr__(self, name):
        if name not in {"owner", "list_favorites", "open_positions", "list_portfolio_snapshot_positions"}:
            raise RuntimeError("UnexpectedGroupAccess")
        return getattr(self.journal, name)


def render_correction(report, clock):
    if seal_daily_score_report(report)["report_id"] != report.get("report_id"):
        raise RuntimeError("UnsealedCorrectionReport")
    local = clock.astimezone(ZoneInfo("Europe/Madrid"))
    subject, plain, html = render_daily_score_email(report,
        app_url=f"https://stock-signal-lab-ddriu.streamlit.app/?digest={report['report_id']}")
    intro = (f"Corrección solicitada: revisión generada el {local:%d/%m/%Y a las %H:%M %Z}. "
        f"Se utilizan precios del cierre del {SESSION:%d/%m/%Y}, la última sesión estadounidense "
        "cerrada con margen de publicación. No son cotizaciones intradía ni un precio de ejecución. "
        "Se conserva el informe anterior; esta revisión tiene su propia versión y no ejecuta operaciones.")
    html, substitutions = re.subn(r"(<body\b[^>]*>)", lambda m: m.group(1) + f"<p>{escape(intro)}</p>", html, count=1)
    if substitutions != 1:
        raise RuntimeError("UnexpectedCorrectionRenderer")
    return "Corrección solicitada · " + subject, intro + "\n\n" + plain, html


def execute(clock=None):
    validate_clock(clock)
    if load_supabase_config() is None:
        raise RuntimeError("MissingPrivateBackend")
    receipts, attempted = [], False
    previous_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            journal = CorrectionJournal(create_journal(OWNER), clock=clock)
            pref = preference(journal)

            def factory(owner):
                if owner == OWNER:
                    return journal
                if owner == GROUP_PORTFOLIO_OWNER and pref.include_group:
                    group = create_journal(owner)
                    if getattr(group, "owner", None) != GROUP_PORTFOLIO_OWNER:
                        raise RuntimeError("UnexpectedGroupOwner")
                    return GroupReadOnly(group)
                raise RuntimeError("UnexpectedOwnerAccess")

            def sender(recipient, _subject, _plain, _html):
                nonlocal attempted
                current_clock = validate_clock(clock)
                current = preference(journal)
                if attempted or recipient != pref.email or current.email != pref.email or journal.report is None:
                    raise RuntimeError("UnexpectedCorrectionRecipient")
                attempted = True
                rendered = render_correction(journal.report, current_clock)
                try:
                    receipt = send_email(recipient, *rendered)
                except Exception as exc:
                    receipt = getattr(exc, "smtp_receipt", None)
                    if accepted(receipt):
                        receipts.append(receipt)
                    raise
                if not accepted(receipt):
                    raise RuntimeError("UnconfirmedCorrectionSMTP")
                receipts.append(receipt)
                return receipt

            result = run_daily_alerts(owner=OWNER, journal_factory=factory, sender=sender,
                today=SESSION, require_current_session=True)
    finally:
        logging.disable(previous_level)
    report = journal.report or {}
    coverage = daily_score_coverage(report.get("rows", []))
    summary = {"status": "smtp_accepted" if len(receipts) == 1 else "correction_not_confirmed",
        "users_checked": result.users_checked, "emails_sent": result.emails_sent,
        "emails_skipped": result.emails_skipped, "report_id": report.get("report_id"),
        "price_session": report.get("as_of"), "coverage": coverage,
        "incidents_count": len(result.errors), "receipts": [asdict(receipt) for receipt in receipts]}
    print(json.dumps(summary, sort_keys=True))
    if result.users_checked != 1 or result.emails_sent != 1 or len(receipts) != 1:
        raise RuntimeError("NoSingleVerifiedCorrectionEmail")
    return summary


if __name__ == "__main__":
    try:
        execute()
    except Exception as exc:
        print(json.dumps({"status": "correction_requires_attention", "error_type": type(exc).__name__}))
        raise SystemExit(1)
