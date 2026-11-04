"""Regression checks for isolated private persistence failures; never SMTP."""
from datetime import date

import pandas as pd
import pytest


def test_snapshot_storage_failure_is_visible_by_exception_class_without_private_detail(monkeypatch, tmp_path):
    from test_alert_runner import _real_closing_digest_case, _verified_growth_info
    from src.alert_runner import run_daily_alerts
    user, frame = _real_closing_digest_case(monkeypatch, tmp_path, ticker="MINE", session="2026-11-03",
        created_at="2026-11-04T01:55:00+00:00", clock="2026-11-04T01:58:00+00:00")
    def unavailable(**kwargs):
        raise RuntimeError("Private provider address/credential must never be copied")
    monkeypatch.setattr(user, "add_analysis_snapshot", unavailable)
    result = run_daily_alerts(owner="synthetic", journal_factory=lambda owner: user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=_verified_growth_info,
        sender=lambda *args: pytest.fail("No transport without a stored observation"),
        today=date(2026, 11, 3), require_current_session=True)
    assert result.emails_sent == 0 and result.emails_skipped == 1
    assert any("no se pudo guardar el análisis (RuntimeError)" in error for error in result.errors)
    assert "Private provider" not in repr(result.errors)
    assert user.list_analysis_snapshots().empty
    with user._connect() as connection:
        assert connection.execute("SELECT count(*) FROM daily_digest_deliveries").fetchone()[0] == 0


def _independent_case(monkeypatch, tmp_path):
    from test_alert_runner import _real_closing_digest_case
    user, frame = _real_closing_digest_case(monkeypatch, tmp_path, ticker="MINE", session="2026-11-03",
        created_at="2026-11-04T01:55:00+00:00", clock="2026-11-04T01:58:00+00:00")
    return user, frame


def _stored_report(user):
    with user._connect() as connection:
        identifier = connection.execute("SELECT report_id FROM daily_score_reports").fetchone()[0]
    return user.get_daily_score_report(identifier)


def _run_independent_case(user, frame, *, sender):
    from test_alert_runner import _verified_growth_info
    from src.alert_runner import run_daily_alerts
    return run_daily_alerts(owner="synthetic", journal_factory=lambda owner: user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=_verified_growth_info,
        sender=sender, today=date(2026, 11, 3), require_current_session=True)


def _receipt():
    from src.email_sender import EmailDeliveryReceipt
    return EmailDeliveryReceipt(status="smtp_accepted", smtp_code=250,
        accepted_at="2026-11-04T01:58:00+00:00", receipt_hash="a" * 64, message_hash="b" * 64)


def test_primary_is_saved_when_independent_comparator_fails_without_score_substitution(monkeypatch, tmp_path):
    from src.analysis_policy import decode_policy_note
    user, frame = _independent_case(monkeypatch, tmp_path)
    def unavailable(**kwargs):
        raise ValueError("Private comparative provider detail")
    monkeypatch.setattr("src.alert_runner.evaluate_entry_opportunity", unavailable)
    messages = []
    def sender(*args):
        messages.append(args)
        return _receipt()

    first = _run_independent_case(user, frame, sender=sender)
    second = _run_independent_case(user, frame, sender=sender)
    snapshots = user.list_analysis_snapshots()
    assert len(snapshots) == 1  # Repeating the same observed policy is idempotent.
    saved = snapshots.iloc[0]
    assert pd.isna(saved.opportunity_score) and saved.opportunity_label == ""
    assert saved.entry_score == 50  # Original technical result is not replaced by growth.
    policy = decode_policy_note(saved.note)
    assert policy["primary"]["score"] == 85
    assert policy["primary"]["data_status"] == "complete"
    assert policy["comparative"]["data_status"] == "partial"
    assert "ValueError" in policy["comparative"]["data_error"]
    assert "Private comparative" not in saved.note
    report = _stored_report(user)
    assert report["rows"][0]["score"] == 85
    assert report["rows"][0]["status"] == "entry_strong"
    assert any("oportunidad parcial" in item["reason"] for item in report["incidents"])
    assert first.emails_sent == 1 and second.emails_sent == 0 and second.emails_skipped == 1
    assert len(messages) == 1 and messages[0][0] == "synthetic@example.invalid"
    assert user.list_operations().empty
    assert first.errors == second.errors == ()


def test_missing_primary_does_not_inherit_comparative_or_technical_score(monkeypatch, tmp_path):
    from src.analysis_policy import decode_policy_note
    user, frame = _independent_case(monkeypatch, tmp_path)
    def unavailable(**kwargs):
        raise ValueError("Synthetic unavailable primary")
    monkeypatch.setattr("src.alert_runner.evaluate_growth_momentum", unavailable)
    messages = []
    result = _run_independent_case(user, frame, sender=lambda *args: messages.append(args) or _receipt())
    saved = user.list_analysis_snapshots().iloc[0]
    assert saved.opportunity_score == 75 and saved.entry_score == 50
    policy = decode_policy_note(saved.note)
    assert policy["primary"]["score"] is None
    assert policy["primary"]["data_status"] != "complete"
    row = _stored_report(user)["rows"][0]
    assert row["score"] is None and row["status"] == "review_data"
    assert row["label"] != "Entrada fuerte"
    assert result.emails_sent == 1 and len(messages) == 1  # Informational, not a new-buy alert.
    assert user.list_operations().empty


def test_both_analyses_unavailable_do_not_create_a_zero_snapshot_or_send_mail(monkeypatch, tmp_path):
    user, frame = _independent_case(monkeypatch, tmp_path)
    def unavailable(**kwargs):
        raise ValueError("Synthetic unavailable analysis")
    monkeypatch.setattr("src.alert_runner.evaluate_growth_momentum", unavailable)
    monkeypatch.setattr("src.alert_runner.evaluate_entry_opportunity", unavailable)
    result = _run_independent_case(user, frame,
        sender=lambda *args: pytest.fail("No transport without an observed analysis"))
    assert user.list_analysis_snapshots().empty
    assert result.emails_sent == 0 and result.emails_skipped == 1
    assert user.list_operations().empty
