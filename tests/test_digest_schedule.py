from datetime import datetime, timezone

import pytest

from src.digest_schedule import daily_digest_window


@pytest.mark.parametrize(("utc", "expected"), [
    ("2026-07-28T21:00:00+00:00", True),
    ("2026-07-28T21:47:00+00:00", True),
    ("2026-07-28T22:00:00+00:00", False),
    ("2026-10-26T21:00:00+00:00", False),
    ("2026-10-26T22:00:00+00:00", True),
    ("2026-10-26T22:35:00+00:00", True),
    ("2026-10-26T23:10:00+00:00", False),
    ("2026-10-24T21:00:00+00:00", False),
    ("2026-10-25T22:00:00+00:00", False),
])
def test_schedule_uses_madrid_dst_and_tolerates_delayed_starts(utc, expected):
    assert daily_digest_window(event_name="schedule", now=datetime.fromisoformat(utc)) is expected


def test_schedule_rejects_unzoned_clock_and_keeps_explicit_dispatch():
    assert not daily_digest_window(event_name="schedule", now=datetime(2026, 10, 26, 23))
    assert daily_digest_window(event_name="workflow_dispatch", now=datetime(2026, 10, 26, 10, tzinfo=timezone.utc))


def test_cli_rejects_wrong_slot_before_accessing_any_owner(monkeypatch):
    import scripts.run_alerts as cli
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setattr(cli, "daily_digest_window", lambda **kwargs: False)
    monkeypatch.setattr(cli, "run_daily_alerts", lambda **kwargs: pytest.fail("No access outside schedule window"))
    assert cli.main() == 0


def test_cli_requires_a_new_session_for_the_scheduled_job(monkeypatch):
    import scripts.run_alerts as cli
    from src.alert_runner import AlertRunSummary
    calls = []
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setattr(cli, "daily_digest_window", lambda **kwargs: True)
    monkeypatch.setattr(cli, "run_daily_alerts", lambda **kwargs: calls.append(kwargs) or AlertRunSummary(
        users_checked=1, tickers_checked=1, emails_sent=0, alerts_sent=0,
        errors=(), tickers_with_prices=1, tickers_with_fresh_prices=1, emails_skipped=1))
    assert cli.main() == 0
    assert calls == [{"require_current_session": True}]
