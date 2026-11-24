"""Delayed scheduled job archives only; no providers or delivery are invoked."""
from types import SimpleNamespace

import pytest

import scripts.run_alerts as cli


def test_actual_delayed_schedule_recovers_archive_without_runner_or_email(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "synthetic-owner")
    monkeypatch.setenv("REPORT_OWNER", "synthetic-owner")
    monkeypatch.setattr(cli, "daily_digest_session_date", lambda: None)
    monkeypatch.setattr(cli, "run_daily_alerts", lambda **_: pytest.fail("No analysis or delivery outside window"))
    journal = SimpleNamespace(owner="synthetic-owner")
    calls = []
    monkeypatch.setattr(cli, "create_journal", lambda owner: calls.append(("journal", owner)) or journal)
    monkeypatch.setattr(cli, "archive_saved_close_report", lambda value: calls.append(("archive", value))
        or SimpleNamespace(status="blocked", session="2026-11-23", authorized=False,
            report_id="a" * 64, error_count=3, eligible_count=2, valid_count=1, unchanged=False))
    assert cli.main() == 0
    assert calls == [("journal", "synthetic-owner"), ("archive", journal)]
    output = capsys.readouterr()
    assert "omitido_fuera_de_horario" in output.out
    assert "archivo=blocked" in output.out
    assert "synthetic-owner" not in output.out + output.err
    assert "cobertura elegible=" not in output.out
    assert "incidencias=" not in output.out


@pytest.mark.parametrize("scope, repository", [(None, "synthetic-owner"),
    ("another-owner", "synthetic-owner"), ("synthetic-owner", None)])
def test_archive_fallback_needs_explicit_matching_repository_owner(monkeypatch, scope, repository):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    for key, value in (("REPORT_OWNER", scope), ("GITHUB_REPOSITORY_OWNER", repository)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli, "daily_digest_session_date", lambda: None)
    monkeypatch.setattr(cli, "create_journal", lambda *_: pytest.fail("No unscoped backend access"))
    monkeypatch.setattr(cli, "run_daily_alerts", lambda **_: pytest.fail("No mail"))
    assert cli.main() == 0


def test_archive_failure_is_visible_without_provider_details_or_mail(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("REPORT_OWNER", "synthetic-owner")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "synthetic-owner")
    monkeypatch.setattr(cli, "daily_digest_session_date", lambda: None)
    monkeypatch.setattr(cli, "create_journal", lambda _: SimpleNamespace(owner="synthetic-owner"))
    monkeypatch.setattr(cli, "run_daily_alerts", lambda **_: pytest.fail("No mail on archive failure"))

    def failed(_journal):
        raise RuntimeError("PRIVATE_BACKEND_TOKEN_OR_BODY")

    monkeypatch.setattr(cli, "archive_saved_close_report", failed)
    assert cli.main() == 1
    output = capsys.readouterr()
    assert "archivo=error" in output.out and "RuntimeError" in output.err
    assert "PRIVATE_BACKEND_TOKEN_OR_BODY" not in output.out + output.err


def test_workflow_declares_only_repository_scoped_report_recovery():
    from pathlib import Path
    workflow = (Path(__file__).parents[1] / ".github/workflows/daily-alerts.yml").read_text()
    assert "REPORT_OWNER: ${{ github.repository_owner }}" in workflow
    assert "steps.stock.outputs.status != 'omitted_outside_window'" in workflow
