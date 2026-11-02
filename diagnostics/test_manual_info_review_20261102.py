"""Offline one-off information-email checks; no SMTP or hosted connection."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "stock-signal-lab-digest-validation"))
from src.alerts import AlertPreferences
from src.daily_digest import DIGEST_VERSION, seal_daily_score_report
from src.email_sender import EmailDeliveryError, _acceptance_receipt

CLOCK = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
PINNED_SOURCE = "9b0ddef62c3003811392550c28026cae5e3a714c75d956d022cbb18594381f16"


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected network, SMTP, scan, model work or real mutation")


@pytest.fixture
def manual(monkeypatch):
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("smtplib.SMTP", forbidden)
    monkeypatch.setattr("smtplib.SMTP_SSL", forbidden)
    monkeypatch.setattr("src.signal_engine.evaluate_latest_signal", forbidden)
    monkeypatch.setattr("src.growth_momentum.evaluate_growth_momentum", forbidden)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("MODEL_RESULTS_ENABLED", "false")
    name = "offline_manual_info_review_20261102"
    spec = spec_from_file_location(name, ROOT / "manual_info_review_20261102.py")
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def report(manual, monkeypatch):
    value = seal_daily_score_report({
        "version": DIGEST_VERSION, "as_of": "2026-10-30", "session_dates": ["2026-10-30"],
        "calendar_status": "complete", "rows": [{"ticker": "OLD_SIGNAL_ONLY", "score": 99}],
        "limitations": [],
    })
    # No chosen content can manufacture the production SHA256 preimage. Unit
    # fixtures keep the exact seal/source contract with a separate test identity.
    monkeypatch.setattr(manual, "SOURCE_REPORT_ID", value["report_id"])
    return value


@pytest.fixture
def context():
    return {"generated_at": CLOCK.isoformat(), "instruments": 2,
            "price_histories": 1, "fundamental_rows": 1,
            "latest_cache_retrieval": "2026-11-02T14:30:00Z", "periodicities": ["annual"],
            "issues": [{"ticker": "SYNTHETIC", "name": "Synthetic <company>", "held": True,
                        "reasons": ["Periodo anual, no último trimestre."]}]}


class Journal:
    def __init__(self, owner="ddriu", preferences=None):
        self.owner = owner
        self.preferences = preferences or AlertPreferences(owner=owner, email="synthetic@example.test", enabled=True)
        self.calls, self.claimed, self.finish = [], set(), []
        self.alert_states = [{"signature": "preserve-daily-throttle"}]
        self.report = None

    def get_alert_preferences(self):
        self.calls.append("preferences")
        return self.preferences

    def get_daily_score_report(self, report_id):
        self.calls.append(("read_report", report_id))
        return deepcopy(self.report)

    def claim_daily_digest(self, key, session, source):
        self.calls.append(("claim", key, session, source))
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    def finish_daily_digest(self, key, status, error_type=""):
        self.finish.append((key, status, error_type))

    def list_favorites(self):
        self.calls.append("favorites")
        return pd.DataFrame([{"ticker": "FAV", "name": "Synthetic favourite"}])

    def list_portfolio_snapshot_positions(self):
        self.calls.append("portfolio_snapshot")
        return pd.DataFrame()

    def open_positions(self):
        self.calls.append("open_positions")
        return pd.DataFrame([{"ticker": "HOLD"}])

    def __getattr__(self, name):
        raise AssertionError("Unexpected journal access: " + name)


def test_pinned_owner_source_request_and_clock(manual):
    assert manual.OWNER == "ddriu"
    assert manual.SOURCE_REPORT_ID == PINNED_SOURCE
    assert manual.REQUEST == "explicit-source-fluency-info-20261102-v1"
    manual.validate_clock(CLOCK)
    manual.validate_clock(datetime.fromisoformat("2026-11-01T23:30:00+00:00"))
    assert len(manual.delivery_key()) == 64
    assert manual.delivery_key() == manual.delivery_key()


@pytest.mark.parametrize("clock", [datetime(2026, 11, 2, 15), datetime.fromisoformat("2026-11-01T12:00+00:00"), datetime.fromisoformat("2026-11-02T23:30+00:00"), None])
def test_clock_rejects_naive_other_madrid_days_and_missing_reference(manual, clock):
    with pytest.raises(RuntimeError):
        manual.validate_clock(clock)


@pytest.mark.parametrize("attempt", ["", "2", "bad"])
def test_repeated_workflow_attempt_stops_before_backend(manual, monkeypatch, attempt):
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", attempt)
    monkeypatch.setattr(manual, "load_supabase_config", forbidden)
    with pytest.raises(RuntimeError, match="RepeatedWorkflowAttempt"):
        manual.execute(CLOCK)


def test_virtual_tracking_enabled_stops_before_backend(manual, monkeypatch):
    monkeypatch.setenv("MODEL_RESULTS_ENABLED", "true")
    monkeypatch.setattr(manual, "load_supabase_config", forbidden)
    with pytest.raises(RuntimeError, match="UnexpectedVirtualTracking"):
        manual.execute(CLOCK)


@pytest.mark.parametrize("failure", ["owner", "preference_owner", "disabled", "recipient_crlf", "recipient_multiple", "recipient_space"])
def test_owner_and_recipient_fail_closed_without_claim_or_transport(manual, report, context, failure):
    journal = Journal()
    if failure == "owner":
        journal.owner = "someone-else"
    elif failure == "preference_owner":
        journal.preferences = replace(journal.preferences, owner="someone-else")
    elif failure == "disabled":
        journal.preferences = replace(journal.preferences, enabled=False)
    else:
        address = {"recipient_crlf": "one@example.test\r\nBcc:other@example.test",
                   "recipient_multiple": "one@example.test,other@example.test",
                   "recipient_space": " synthetic@example.test"}[failure]
        journal.preferences = replace(journal.preferences, email=address)
    with pytest.raises(RuntimeError):
        manual._send(journal, report, context, transport=forbidden)
    assert not journal.claimed and not journal.finish


def test_report_must_have_matching_source_identity_and_valid_original_seal(manual, report, context):
    for value in [dict(report, report_id="a" * 64), dict(report, as_of="2026-11-02")]:
        journal = Journal()
        with pytest.raises(RuntimeError, match="UnexpectedOrUnsealedSourceReport"):
            manual._send(journal, value, context, transport=forbidden)
        assert not journal.claimed


def test_renderer_marks_historical_session_and_current_audit_without_inventing_scores(manual, report, context):
    before, before_context = deepcopy(report), deepcopy(context)
    subject, plain, html = manual.render_info_email(report, context)
    assert report == before and context == before_context
    assert "02/11/2026" in subject
    assert "2026-10-30" in plain
    assert "No es un escaneo intradía nuevo" in plain
    assert "no son recomendaciones actuales" in plain
    assert "OLD_SIGNAL_ONLY" not in plain + html  # historical scores not reissued today
    assert "Synthetic &lt;company&gt;" in html and "Synthetic <company>" not in html
    assert "1,559" in plain and "no representa tu cartera importada" in plain
    assert "?digest=" + report["report_id"] in html


def test_accepted_receipt_sends_once_to_current_preference_and_preserves_original_data(manual, report, context):
    journal, sent = Journal(), []
    before_report, before_context = deepcopy(report), deepcopy(context)
    throttle = deepcopy(journal.alert_states)

    def transport(recipient, *content):
        sent.append((recipient, content))
        return _acceptance_receipt(*content)

    first = manual._send(journal, report, context, transport=transport)
    repeated = manual._send(journal, report, context, transport=transport)
    assert first["status"] == "smtp_accepted" and first["emails_sent"] == 1
    assert first["receipts"][0]["smtp_code"] == 250
    assert repeated["status"] == "already_reserved_or_completed" and repeated["emails_sent"] == 0
    assert len(sent) == 1 and sent[0][0] == journal.preferences.email
    assert journal.finish == [(manual.delivery_key(), "sent", "")]
    assert journal.alert_states == throttle and report == before_report and context == before_context
    assert "synthetic@example.test" not in json.dumps(first)


@pytest.mark.parametrize("failure", ["none", "exception", "post_quit", "wrong_code"])
def test_ambiguous_receipts_never_become_fake_success_or_automatic_retries(manual, report, context, failure):
    journal, attempts = Journal(), []

    def transport(recipient, *content):
        attempts.append(recipient)
        receipt = _acceptance_receipt(*content)
        if failure == "none":
            return None
        if failure == "wrong_code":
            return replace(receipt, smtp_code=251)
        error = EmailDeliveryError("synthetic transport error")
        if failure == "post_quit":
            error.smtp_receipt = receipt
        raise error

    result = manual._send(journal, report, context, transport=transport)
    assert result["emails_sent"] == int(failure == "post_quit")
    assert result["status"] == ("smtp_accepted" if failure == "post_quit" else "delivery_unconfirmed")
    assert journal.finish[0][1] == ("sent" if failure == "post_quit" else "uncertain")
    assert manual._send(journal, report, context, transport=transport)["emails_sent"] == 0
    assert len(attempts) == 1


def test_preference_change_after_claim_does_not_send_to_either_recipient(manual, report, context):
    journal = Journal()
    original = journal.get_alert_preferences
    reads = []

    def preferences():
        reads.append(True)
        return original() if len(reads) == 1 else replace(journal.preferences, email="changed@example.test")

    journal.get_alert_preferences = preferences
    result = manual._send(journal, report, context, transport=forbidden)
    assert result["emails_sent"] == 0 and result["status"] == "delivery_unconfirmed"
    assert journal.finish[0][1:] == ("uncertain", "RuntimeError")


def test_terminal_persistence_failure_never_reopens_accepted_delivery(manual, report, context):
    journal, sent = Journal(), []

    def finish(*args, **kwargs):
        raise RuntimeError("ambiguous final persistence")

    def transport(recipient, *content):
        sent.append(recipient)
        return _acceptance_receipt(*content)

    journal.finish_daily_digest = finish
    result = manual._send(journal, report, context, transport=transport)
    assert result["terminal_saved"] is False and result["emails_sent"] == 1
    assert manual._send(journal, report, context, transport=forbidden)["emails_sent"] == 0
    assert len(sent) == 1


def test_context_only_reads_requested_owner_cache_and_preserves_dates_and_quality(manual, monkeypatch):
    journal = Journal()
    calls = []

    class Cache:
        def __init__(self, owner_journal):
            assert owner_journal is journal

        def heads(self):
            calls.append("heads")
            return [{"kind": "prices", "symbol": "HOLD", "observed_at": "2026-11-02T14:00:00Z",
                     "metadata": {"completed_price_date": "2026-10-30", "quote": {"currency": "USD"}}}]

        def read_many(self, kind, symbols):
            calls.append((kind, symbols))
            return [{"symbol": "HOLD", "payload": {"_metric_periods": {
                "revenueGrowth": {"periodicity": "annual", "period_end": "2024-01-01", "comparable": False}}}}]

    monkeypatch.setattr(manual, "MarketCacheStore", Cache)
    monkeypatch.setattr(manual, "assess_analysis_data_quality", lambda value, as_of: {"block_entry": True, "reasons": ["Fuente insuficiente"]})
    context = manual.gather_context(journal, CLOCK)
    assert context["instruments"] == 2 and context["price_histories"] == context["fundamental_rows"] == 1
    assert context["periodicities"] == ["annual"]
    assert context["issues"][0]["ticker"] == "HOLD" and context["issues"][0]["held"] is True
    assert any("Periodo antiguo" in reason for reason in context["issues"][0]["reasons"])
    assert calls == ["heads", ("fundamentals", ["FAV", "HOLD"])]
    assert not journal.claimed and not journal.finish


def test_execute_has_only_one_owner_and_prints_counts_not_private_recipient(manual, report, context, monkeypatch, capsys):
    journal, owners, sends = Journal(), [], []
    journal.report = report
    monkeypatch.setattr(manual, "load_supabase_config", lambda: object())
    monkeypatch.setattr(manual, "create_journal", lambda owner: owners.append(owner) or journal)
    monkeypatch.setattr(manual, "gather_context", lambda owner_journal, clock: deepcopy(context))

    def transport(recipient, *content):
        sends.append(recipient)
        return _acceptance_receipt(*content)

    monkeypatch.setattr(manual, "send_email", transport)
    result = manual.execute(CLOCK)
    assert owners == ["ddriu"] and sends == [journal.preferences.email]
    assert result["emails_sent"] == 1
    output = capsys.readouterr()
    parsed = json.loads(output.out)
    assert parsed["status"] == "smtp_accepted"
    assert journal.preferences.email not in output.out + output.err
    assert "Synthetic <company>" not in output.out + output.err
