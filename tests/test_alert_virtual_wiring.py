"""Deployment scope and signal/virtual/mail wiring; no backend or SMTP."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import run_alerts as cli
from scripts import run_model_tracking as tracking
from src.alert_runner import AlertRunSummary


@pytest.mark.parametrize("enabled,owner", [("", "scope"), ("false", "scope"), ("0", "scope"), ("true", "")])
def test_tracker_configuration_fails_closed_without_enabled_and_owner(monkeypatch, enabled, owner):
    monkeypatch.setenv("MODEL_RESULTS_ENABLED", enabled)
    monkeypatch.setenv("MODEL_RESULTS_OWNER", owner)
    assert cli.configured_virtual_tracker() is None


def test_scope_cannot_advance_another_recipient(monkeypatch):
    monkeypatch.setenv("MODEL_RESULTS_ENABLED", "true")
    monkeypatch.setenv("MODEL_RESULTS_OWNER", "scope")
    collect = Mock(return_value={"status": "updated"})
    monkeypatch.setattr(tracking, "collect_owner_models", collect)
    callback = cli.configured_virtual_tracker()
    assert callback(SimpleNamespace(owner="other"), []) is None
    collect.assert_not_called()
    journal = SimpleNamespace(owner="scope")
    assert callback(journal, ["saved"]) == {"status": "updated"}
    collect.assert_called_once_with(journal, ["saved"])


def test_cli_passes_tracker_to_same_analysis_send_pipeline(monkeypatch):
    callback = Mock()
    monkeypatch.setattr(cli, "configured_virtual_tracker", lambda: callback)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    run = Mock(return_value=AlertRunSummary(0, 0, 0, 0, ()))
    monkeypatch.setattr(cli, "run_daily_alerts", run)
    assert cli.main() == 0
    run.assert_called_once_with(virtual_tracker=callback)
