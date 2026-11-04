"""Regression checks for isolated private persistence failures; never SMTP."""
from datetime import date

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
