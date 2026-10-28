from datetime import date, datetime, timezone

import pytest

from src.digest_schedule import daily_digest_session_date, daily_digest_window


@pytest.mark.parametrize(("utc", "session"), [
    ("2026-07-28T21:00:00+00:00", "2026-07-28"),
    ("2026-07-28T21:47:00+00:00", "2026-07-28"),
    ("2026-07-28T22:00:00+00:00", "2026-07-28"),
    ("2026-07-29T03:59:59+00:00", "2026-07-28"),
    ("2026-07-29T04:00:00+00:00", None),
    ("2026-10-26T21:00:00+00:00", None),
    ("2026-10-26T22:00:00+00:00", "2026-10-26"),
    ("2026-10-26T22:35:00+00:00", "2026-10-26"),
    ("2026-10-26T23:10:00+00:00", "2026-10-26"),
    # Actual production starts that previously returned a misleading success.
    ("2026-10-27T01:05:44+00:00", "2026-10-26"),
    ("2026-10-28T00:58:19+00:00", "2026-10-27"),
    ("2026-10-28T04:59:59+00:00", "2026-10-27"),
    ("2026-10-28T05:00:00+00:00", None),
    # Friday's report can finish on Saturday, not Sunday or Monday morning.
    ("2026-10-23T22:00:00+00:00", "2026-10-23"),
    ("2026-10-24T03:59:59+00:00", "2026-10-23"),
    ("2026-10-24T04:00:00+00:00", None),
    ("2026-10-24T21:00:00+00:00", None),
    ("2026-10-25T00:30:00+00:00", None),
    ("2026-10-25T01:30:00+00:00", None),
    ("2026-10-25T22:00:00+00:00", None),
    ("2026-10-26T00:30:00+00:00", None),
    # Month/year boundaries must retain the actual preceding session date.
    ("2026-07-01T00:00:00+00:00", "2026-06-30"),
    ("2027-01-01T00:00:00+00:00", "2026-12-31"),
])
def test_schedule_uses_madrid_session_date_across_midnight_and_dst(utc, session):
    clock = datetime.fromisoformat(utc)
    target = date.fromisoformat(session) if session else None
    assert daily_digest_session_date(now=clock) == target
    assert daily_digest_window(event_name="schedule", now=clock) is (target is not None)


def test_schedule_rejects_unzoned_clock_and_keeps_explicit_dispatch():
    assert daily_digest_session_date(now=datetime(2026, 10, 26, 23)) is None
    assert not daily_digest_window(event_name="schedule", now=datetime(2026, 10, 26, 23))
    assert daily_digest_window(event_name="workflow_dispatch", now=datetime(2026, 10, 26, 10, tzinfo=timezone.utc))


def test_cli_rejects_wrong_slot_visibly_before_accessing_any_owner(monkeypatch, capsys):
    import scripts.run_alerts as cli
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setattr(cli, "daily_digest_session_date", lambda: None)
    monkeypatch.setattr(cli, "run_daily_alerts", lambda **kwargs: pytest.fail("No access outside schedule window"))
    assert cli.main() == 1
    captured = capsys.readouterr()
    assert "::error::" in captured.err
    assert "06:00" in captured.err


@pytest.mark.parametrize("session", [date(2026, 10, 27), date(2026, 10, 23)])
def test_cli_requires_target_session_not_calendar_day_of_delayed_job(monkeypatch, session, capsys):
    import scripts.run_alerts as cli
    from src.alert_runner import AlertRunSummary
    calls = []
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setattr(cli, "daily_digest_session_date", lambda: session)
    monkeypatch.setattr(cli, "run_daily_alerts", lambda **kwargs: calls.append(kwargs) or AlertRunSummary(
        users_checked=1, tickers_checked=1, emails_sent=0, alerts_sent=0,
        errors=(), tickers_with_prices=1, tickers_with_fresh_prices=1, emails_skipped=1))
    assert cli.main() == 0
    assert calls == [{"today": session, "require_current_session": True}]
    assert session.isoformat() in capsys.readouterr().out


def test_manual_dispatch_does_not_backdate_or_bypass_runner_claim(monkeypatch):
    import scripts.run_alerts as cli
    from src.alert_runner import AlertRunSummary
    calls = []
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setattr(cli, "daily_digest_session_date", lambda: pytest.fail("Manual review uses its actual data"))
    monkeypatch.setattr(cli, "run_daily_alerts", lambda **kwargs: calls.append(kwargs) or AlertRunSummary(0, 0, 0, 0, ()))
    assert cli.main() == 0
    assert calls == [{}]


def test_workflow_preserves_daily_schedule_and_adds_bounded_recovery():
    from pathlib import Path
    workflow = (Path(__file__).parents[1] / ".github/workflows/daily-alerts.yml").read_text()
    assert 'cron: "0 23 * * 1-5"' in workflow
    assert 'cron: "17 1 * * 2-6"' in workflow
    assert workflow.count('timezone: "Europe/Madrid"') == 2
    assert "cancel-in-progress: false" in workflow
