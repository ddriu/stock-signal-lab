from src.alert_runner import AlertRunSummary
from scripts.run_alerts import main


def test_daily_job_fails_when_every_download_is_stale_or_missing(monkeypatch) -> None:
    monkeypatch.setattr("scripts.run_alerts.run_daily_alerts", lambda: AlertRunSummary(
        users_checked=1, tickers_checked=136, emails_sent=1, alerts_sent=0,
        errors=("Precios antiguos",), tickers_with_prices=136, tickers_with_fresh_prices=0,
    ))
    assert main() == 1


def test_daily_job_fails_when_no_requested_email_was_delivered(monkeypatch) -> None:
    monkeypatch.setattr("scripts.run_alerts.run_daily_alerts", lambda: AlertRunSummary(
        users_checked=1, tickers_checked=136, emails_sent=0, alerts_sent=0,
        errors=("SMTP no disponible",), tickers_with_prices=136, tickers_with_fresh_prices=136,
    ))
    assert main() == 1


def test_daily_job_keeps_partial_delivery_visible_without_retrying(monkeypatch) -> None:
    monkeypatch.setattr("scripts.run_alerts.run_daily_alerts", lambda: AlertRunSummary(
        users_checked=2, tickers_checked=136, emails_sent=1, alerts_sent=0,
        errors=("Un ticker no disponible",), tickers_with_prices=135, tickers_with_fresh_prices=135,
    ))
    assert main() == 0


def test_daily_job_without_enabled_recipients_is_a_valid_noop(monkeypatch) -> None:
    monkeypatch.setattr("scripts.run_alerts.run_daily_alerts", lambda: AlertRunSummary(0, 0, 0, 0, ()))
    assert main() == 0


def test_public_workflow_logs_do_not_publish_owner_addresses_or_backend_details(monkeypatch, capsys):
    monkeypatch.setattr("scripts.run_alerts.run_daily_alerts", lambda: AlertRunSummary(
        users_checked=1, tickers_checked=1, emails_sent=0, alerts_sent=0,
        errors=("private-owner@example.invalid: backend private-token-123",),
        tickers_with_prices=1, tickers_with_fresh_prices=1,
    ))
    assert main() == 1
    captured = capsys.readouterr()
    assert "1 incidencias" in captured.err
    assert "private-owner" not in captured.err + captured.out
    assert "private-token" not in captured.err + captured.out


def test_successful_stock_summary_does_not_attempt_downstream_arena_mail(monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setattr("scripts.run_alerts.run_daily_alerts", lambda: AlertRunSummary(
        users_checked=3, tickers_checked=185, emails_sent=2, alerts_sent=0,
        errors=("Synthetic partial-price incident",),
        tickers_with_prices=183, tickers_with_fresh_prices=179,
    ))
    def forbidden(*args, **kwargs):
        raise AssertionError("Arena must run only in its separate workflow step")
    monkeypatch.setattr("scripts.send_arena_digest.send_arena_digest", forbidden)
    assert main() == 0


def test_scheduled_stage_exports_only_original_market_date(monkeypatch, tmp_path):
    from datetime import date
    path = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setattr("scripts.run_alerts.daily_digest_session_date", lambda: date(2026,11,12))
    def review(**kwargs):
        assert kwargs["today"] == date(2026,11,12) and kwargs["require_current_session"]
        return AlertRunSummary(0,0,0,0,())
    monkeypatch.setattr("scripts.run_alerts.run_daily_alerts", review)
    assert main() == 0
    assert path.read_text() == "market_session=2026-11-12\n"


def test_late_skipped_stage_tells_arena_not_to_run(monkeypatch, tmp_path):
    path = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setattr("scripts.run_alerts.daily_digest_session_date", lambda: None)
    assert main() == 0
    assert path.read_text() == "status=omitted_outside_window\n"
