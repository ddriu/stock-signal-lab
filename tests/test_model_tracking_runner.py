"""Scheduling switches and per-experiment isolation, without network or mail."""
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd

from scripts import run_model_tracking as runner
from src.model_results_tracking import TRACKING_VERSION


def test_disabled_scheduler_has_no_storage_or_mail_side_effects(monkeypatch):
    monkeypatch.delenv("MODEL_RESULTS_ENABLED", raising=False)
    factory = Mock(side_effect=AssertionError("must remain inert"))
    monkeypatch.setattr(runner, "create_journal", factory)
    assert runner.main() == 0
    factory.assert_not_called()


def test_incomplete_experiment_does_not_block_another_for_same_owner(monkeypatch, capsys):
    monkeypatch.setenv("MODEL_RESULTS_ENABLED", "true")
    backend = SimpleNamespace(url="https://example.invalid", _list_rows=Mock(
        return_value=[{"owner": "private-user", "season_id": "s"}]))
    journal = SimpleNamespace(list_analysis_snapshots=lambda: pd.DataFrame())
    headers = [{"manifest": {"experiment_id": name, "engine_version": TRACKING_VERSION}}
               for name in ("partial", "complete")]
    monkeypatch.setattr(runner, "create_journal", lambda owner:
        backend if owner == runner.GROUP_PORTFOLIO_OWNER else journal)
    monkeypatch.setattr(runner, "create_model_results_store", lambda journal:
        SimpleNamespace(list_seasons=lambda: headers))
    calls = []
    def advance(journal, store, seasons, snapshots, experiment, manifest):
        calls.append(experiment)
        if experiment == "partial":
            raise ValueError("private diagnostic")
        return {"runs": 8}
    monkeypatch.setattr(runner, "advance_one", advance)
    assert runner.main() == 1  # Fault remains visible, never a silent success.
    assert calls == ["partial", "complete"]
    output = capsys.readouterr()
    assert "8 resultados nuevos" in output.out
    assert "private-user" not in output.out + output.err
    assert "private diagnostic" not in output.out + output.err


def test_partial_activation_fails_before_downloading(monkeypatch):
    download = Mock(side_effect=AssertionError("no download"))
    monkeypatch.setattr(runner, "collect_model_market_bundle", download)
    import pytest
    with pytest.raises(ValueError, match="Incomplete activation"):
        runner.advance_one(None, None, [{"manifest": {"experiment_id": "e", "mode": "budget", "strategy_key": "A"}}],
            pd.DataFrame(), "e", {})
    download.assert_not_called()
