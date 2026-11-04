"""Offline guards for one corrected email; no provider, SMTP or account access."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest

import manual_corrected_review_20261104 as review
from src.alert_runner import AlertRunSummary
from src.alerts import normalize_alert_preferences
from src.daily_digest import seal_daily_score_report
from src.email_sender import EmailDeliveryReceipt

CLOCK = datetime.fromisoformat("2026-11-04T21:30:00+00:00")


@pytest.fixture(autouse=True)
def guarded_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("MODEL_RESULTS_ENABLED", "false")


def report(**changes):
    value = {"as_of": "2026-11-04", "version": "test", "rows": [
        {"ticker": "PRIVATE", "company_name": "Private Name <script>", "score": 85,
         "label": "Entrada fuerte", "held": False, "status": "entry_strong",
         "quality": {"block_entry": False}, "history": []}]}
    value.update(changes)
    return seal_daily_score_report(value)


class Journal:
    owner = "ddriu"
    def __init__(self):
        self.pref = normalize_alert_preferences(owner=self.owner, email="private@example.invalid",
            enabled=True, include_group=False)
        self.claims, self.reports, self.finishes = {}, {}, []
    def get_alert_preferences(self):
        return self.pref
    def save_daily_score_report(self, value):
        self.reports[value["report_id"]] = deepcopy(value)
        return value["report_id"]
    def claim_daily_digest(self, key, market_date, report_id):
        if key in self.claims:
            return False
        self.claims[key] = (market_date, report_id)
        return True
    def finish_daily_digest(self, key, status, *, error_type=""):
        self.finishes.append((key, status, error_type))


def receipt(code=250):
    return EmailDeliveryReceipt(status="smtp_accepted", smtp_code=code,
        accepted_at=CLOCK.isoformat(), receipt_hash="a" * 64, message_hash="b" * 64)


@pytest.mark.parametrize("stamp", ["2026-11-03T18:00:00+00:00", "2026-11-04T21:19:59+00:00",
    "2026-11-05T11:00:00+00:00", "2026-11-05T22:00:00+00:00", "2026-11-04T21:30:00"])
def test_expiry_unknown_clock_or_newer_completed_session_blocks_before_backend(monkeypatch, stamp):
    monkeypatch.setattr(review, "load_supabase_config", lambda: pytest.fail("No backend check before valid clock"))
    with pytest.raises(RuntimeError):
        review.execute(datetime.fromisoformat(stamp))


def test_clock_uses_shared_publication_margin_and_calendar():
    assert review.validate_clock(CLOCK) == CLOCK
    assert review.validate_clock(datetime.fromisoformat("2026-11-04T21:20:00+00:00"))
    assert review.validate_clock(datetime.fromisoformat("2026-11-05T10:59:59+00:00"))


def test_session_calendar_is_anchored_to_current_madrid_date_after_midnight(monkeypatch):
    calls = []
    monkeypatch.setattr(review, "_sessions", lambda ticker, anchor, clock: calls.append((ticker, anchor, clock)) or (["2026-11-04"], "XNYS"))
    assert review.validate_clock(datetime.fromisoformat("2026-11-04T23:05:00+00:00"))
    assert calls[0][1] == "2026-11-05"
    monkeypatch.setattr(review, "_sessions", lambda *args: (["2026-11-03"], "XNYS"))
    with pytest.raises(RuntimeError, match="CorrectionTargetNeedsReview"):
        review.validate_clock(CLOCK)


@pytest.mark.parametrize(("key", "value"), [("GITHUB_RUN_ATTEMPT", "2"), ("MODEL_RESULTS_ENABLED", "true")])
def test_repeated_attempt_or_model_tracking_blocks(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError):
        review.validate_clock(CLOCK)


@pytest.mark.parametrize("changes", [dict(owner="other"), dict(enabled=False),
    dict(email="invalid"), dict(email=" private@example.invalid"), dict(email="a@example.invalid\r\nBcc: other@example.invalid")])
def test_preference_scope_and_recipient_validation(changes):
    journal = Journal()
    journal.pref = replace(journal.pref, **changes)
    with pytest.raises(RuntimeError):
        review.preference(journal)


def test_namespace_stable_independent_of_report_and_preserves_original_delivery():
    journal = Journal()
    journal.claims["0" * 64] = ("2026-11-03", "old")
    wrapped = review.CorrectionJournal(journal, clock=CLOCK)
    value = report()
    wrapped.save_daily_score_report(value)
    assert wrapped.claim_daily_digest("a" * 64, "2026-11-04", value["report_id"])
    assert not wrapped.claim_daily_digest("b" * 64, "2026-11-04", value["report_id"])
    wrapped.finish_daily_digest("a" * 64, "sent")
    assert journal.claims["0" * 64] == ("2026-11-03", "old")
    assert journal.finishes == [(review.correction_key(), "sent", "")]


def test_correction_identity_does_not_change_with_selected_price_session(monkeypatch):
    identity = review.correction_key()
    monkeypatch.setattr(review, "SESSION", review.date(2026, 11, 3))
    assert review.correction_key() == identity


@pytest.mark.parametrize("bad", [report(as_of="2026-11-02"), report(virtual_tracking={}), report(rows=[]),
    {**report(), "report_id": "a" * 64}])
def test_invalid_unobserved_or_virtual_report_never_gets_a_claim(bad):
    journal = Journal()
    wrapped = review.CorrectionJournal(journal, clock=CLOCK)
    with pytest.raises(RuntimeError):
        wrapped.save_daily_score_report(bad)
    assert not journal.claims and not journal.reports


def test_renderer_is_explicit_about_price_session_generation_time_and_correction():
    value = report()
    before = deepcopy(value)
    subject, plain, html = review.render_correction(value, CLOCK)
    assert "Corrección solicitada" in subject
    assert "04/11/2026" in plain and "03/11/2026" not in plain
    assert "última sesión estadounidense" in plain and "No son cotizaciones intradía" in html
    assert value["report_id"] in plain and value["report_id"] in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert value == before


def prepare_execute(monkeypatch, *, change_recipient=False):
    journal, delivered = Journal(), []
    monkeypatch.setattr(review, "load_supabase_config", lambda: object())
    monkeypatch.setattr(review, "create_journal", lambda owner: journal if owner == "ddriu" else pytest.fail("Unexpected owner"))
    def runner(**kwargs):
        assert kwargs["owner"] == "ddriu"
        assert kwargs["today"] == review.SESSION and kwargs["require_current_session"] is True
        assert "virtual_tracker" not in kwargs
        wrapped = kwargs["journal_factory"]("ddriu")
        with pytest.raises(RuntimeError):
            kwargs["journal_factory"]("unrelated")
        value = report()
        wrapped.save_daily_score_report(value)
        if not wrapped.claim_daily_digest("a" * 64, "2026-11-04", value["report_id"]):
            return AlertRunSummary(1, 1, 0, 0, (), 1, 1, 1)
        if change_recipient:
            journal.pref = replace(journal.pref, email="changed@example.invalid")
        try:
            result = kwargs["sender"]("private@example.invalid", "discarded", "discarded", "discarded")
        except Exception as exc:
            result = getattr(exc, "smtp_receipt", None)
            wrapped.finish_daily_digest("a" * 64, "sent" if review.accepted(result) else "uncertain", error_type=type(exc).__name__)
            return AlertRunSummary(1, 1, int(review.accepted(result)), 0, (type(exc).__name__,), 1, 1)
        wrapped.finish_daily_digest("a" * 64, "sent")
        return AlertRunSummary(1, 1, 1, 0, (), 1, 1)
    monkeypatch.setattr(review, "run_daily_alerts", runner)
    monkeypatch.setattr(review, "send_email", lambda *args: delivered.append(args) or receipt())
    return journal, delivered


def test_single_request_sends_only_current_owner_and_logs_no_pii(monkeypatch, capsys):
    journal, delivered = prepare_execute(monkeypatch)
    result = review.execute(CLOCK)
    assert result["status"] == "smtp_accepted" and len(delivered) == 1
    assert delivered[0][0] == journal.pref.email
    assert "Corrección solicitada" in delivered[0][1]
    output = capsys.readouterr().out
    assert "Private Name" not in output and journal.pref.email not in output
    assert journal.finishes[0][1] == "sent"
    with pytest.raises(RuntimeError, match="NoSingleVerifiedCorrectionEmail"):
        review.execute(CLOCK)
    assert len(delivered) == 1


def test_recipient_change_before_transport_does_not_send_or_retry(monkeypatch):
    journal, delivered = prepare_execute(monkeypatch, change_recipient=True)
    with pytest.raises(RuntimeError):
        review.execute(CLOCK)
    assert not delivered and journal.finishes[0][1] == "uncertain"


@pytest.mark.parametrize("unconfirmed", [None, receipt(251), {}])
def test_smtp_without_250_receipt_is_uncertain_and_never_resent(monkeypatch, unconfirmed):
    journal, delivered = prepare_execute(monkeypatch)
    monkeypatch.setattr(review, "send_email", lambda *args: delivered.append(args) or unconfirmed)
    with pytest.raises(RuntimeError):
        review.execute(CLOCK)
    assert len(delivered) == 1 and journal.finishes[0][1] == "uncertain"
    with pytest.raises(RuntimeError):
        review.execute(CLOCK)
    assert len(delivered) == 1


def test_post_data_transport_exception_preserves_accepted_receipt(monkeypatch):
    journal, _ = prepare_execute(monkeypatch)
    def accepted_then_failed(*args):
        error = RuntimeError("Private transport detail")
        error.smtp_receipt = receipt()
        raise error
    monkeypatch.setattr(review, "send_email", accepted_then_failed)
    assert review.execute(CLOCK)["status"] == "smtp_accepted"
    assert journal.finishes[0][1] == "sent"


def test_group_wrapper_is_read_only_and_no_trade_method_is_exposed():
    wrapped = review.CorrectionJournal(Journal(), clock=CLOCK)
    for method in ("add_operation", "delete_operation", "save_paper_daily_run"):
        with pytest.raises(RuntimeError):
            getattr(wrapped, method)
    group = review.GroupReadOnly(SimpleNamespace(owner="grupo_compartido", list_favorites=lambda: "allowed"))
    assert group.list_favorites() == "allowed"
    with pytest.raises(RuntimeError):
        group.claim_daily_digest
