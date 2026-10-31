"""Measurement readiness is not trading, retroactive funding or delivery."""
from copy import deepcopy
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from scripts import run_model_tracking as runner
from src.journal import TradingJournal
from src.model_results_store import ModelResultsStore, ModelResultsConflict
import src.model_results_tracking as tracking


@pytest.fixture
def activated(tmp_path):
    journal = TradingJournal(tmp_path / "readiness.db", owner="synthetic-owner")
    identifier = tracking.activate_model_experiment(journal, {"audit": "synthetic"},
        now="2026-10-19T19:00:00Z")
    return journal, identifier, ModelResultsStore(journal)


def test_explicit_activation_records_only_real_clock_starting_cash_not_performance(activated):
    journal, identifier, store = activated
    records = store.select("snapshots")
    assert len(records) == 8
    headers = {row["season_id"]: row["manifest"] for row in store.list_seasons()}
    for row in records:
        snapshot, manifest = row["snapshot"], headers[row["season_id"]]
        assert snapshot["snapshot_kind"] == "activation"
        assert snapshot["available_at"].startswith("2026-10-19T19:00")
        assert snapshot["metrics"]["final_value_usd"] == manifest["initial_cash_usd"]
        assert snapshot["metrics"]["net_return_pct"] is None
        assert not snapshot["coverage_complete"]
        assert snapshot["daily"] == snapshot["lots"] == snapshot["trades"] == []
    assert store.select("events") == []
    assert journal.list_operations().empty and journal.list_paper_simulations().empty


def test_initialization_restart_is_idempotent_and_preserves_original_clock(activated):
    journal, identifier, store = activated
    before = {kind: deepcopy(store.select(kind)) for kind in ("seasons", "batches", "events", "snapshots")}
    tracking.initialize_model_experiment(journal, identifier)
    tracking.resume_model_activation(journal, identifier, now="2026-10-19T19:01:00Z")
    assert before == {kind: store.select(kind) for kind in before}


def test_initial_snapshot_crash_resumes_missing_rows_without_restarting_activation(tmp_path, monkeypatch):
    journal = TradingJournal(tmp_path / "interrupted.db", owner="synthetic-owner")
    append = tracking._append
    count = 0
    def interrupted(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 4:
            raise RuntimeError("synthetic interruption")
        return append(*args, **kwargs)
    monkeypatch.setattr(tracking, "_append", interrupted)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        tracking.activate_model_experiment(journal, {"audit": "synthetic"},
            now="2026-10-19T19:00:00Z")
    store = ModelResultsStore(journal)
    assert len(store.list_seasons()) == 8 and len(store.select("snapshots")) == 3
    before = deepcopy(store.select("snapshots"))
    identifier = store.list_seasons()[0]["manifest"]["experiment_id"]
    monkeypatch.setattr(tracking, "_append", append)
    tracking.resume_model_activation(journal, identifier, now="2026-10-19T19:01:00Z")
    after = store.select("snapshots")
    assert len(after) == 8 and all(row in after for row in before)
    assert all(row["snapshot"]["activation_at"].startswith("2026-10-19T19:00") for row in after)
    new = [row for row in after if row not in before]
    assert all(row["snapshot"]["available_at"].startswith("2026-10-19T19:01") for row in new)


def test_engine_mismatch_is_diagnosed_before_public_download_or_mutation(activated, monkeypatch):
    journal, identifier, store = activated
    headers = store.list_seasons()
    before = deepcopy(store.select("snapshots"))
    monkeypatch.setattr(tracking, "engine_identity", lambda: {"signature": "new-runtime"})
    state = tracking.experiment_readiness(headers)
    assert state == {"status": "frozen_engine_changed", "ready": False, "requires_new_experiment": True}
    download = Mock(side_effect=AssertionError("must not download"))
    monkeypatch.setattr(runner, "collect_model_market_bundle", download)
    with pytest.raises(ModelResultsConflict, match="incompatible"):
        runner.advance_one(journal, store, headers, pd.DataFrame(), identifier, headers[0]["manifest"])
    download.assert_not_called()
    assert store.select("snapshots") == before


def test_current_headers_ready_partial_and_mixed_headers_never_approved(activated):
    _, _, store = activated
    headers = store.list_seasons()
    assert tracking.experiment_readiness(headers)["ready"]
    assert tracking.experiment_readiness(headers[:-1])["status"] == "partial_activation"
    changed = deepcopy(headers)
    changed[-1]["manifest"]["source"] = "growth"
    assert tracking.experiment_readiness(changed)["status"] == "inconsistent_headers"


def test_growth_identity_freezes_generator_without_changing_technical_scope(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "config.py").write_text("synthetic-config")
    growth = tmp_path / "src/growth_momentum.py"
    growth.write_text("synthetic-growth-v1")
    monkeypatch.setattr(tracking, "_ROOT", tmp_path)
    technical = tracking.engine_identity()
    raw = tracking.engine_identity("growth", "raw")
    verified = tracking.engine_identity("growth", "verified")
    assert raw["signature"] != verified["signature"]
    assert "src/growth_momentum.py" in raw["files"]
    assert "src/growth_momentum.py" not in technical["files"]
    growth.write_text("synthetic-growth-v2")
    assert tracking.engine_identity()["signature"] == technical["signature"]
    assert tracking.engine_identity("growth", "raw")["signature"] != raw["signature"]


def test_verified_activation_must_freeze_same_retrospective_cohort(tmp_path):
    journal = TradingJournal(tmp_path / "cohort.db", owner="synthetic-owner")
    with pytest.raises(ValueError, match="cohort"):
        tracking.activate_model_experiment(journal, {"audit": "raw-only"}, source="growth",
            cohort="verified", now="2026-10-19T19:00:00Z")
    assert ModelResultsStore(journal).list_seasons() == []


@pytest.mark.parametrize("audit", [
    {"source": "technical", "cohort": "verified", "cohort_version": "recorded-quality-first-observation-v1"},
    {"source": "growth", "cohort": "verified", "cohort_version": "unknown-gate"},
])
def test_verified_activation_rejects_unrelated_source_or_unknown_gate_without_writes(tmp_path, audit):
    journal = TradingJournal(tmp_path / "wrong-audit.db", owner="synthetic-owner")
    with pytest.raises(ValueError, match="source|cohort"):
        tracking.activate_model_experiment(journal, audit, source="growth", cohort="verified",
            now="2026-10-19T19:00:00Z")
    assert ModelResultsStore(journal).list_seasons() == []


def test_raw_and_verified_growth_activations_are_separate_immutable_experiments(tmp_path):
    from src.strategy_audit_data import VERIFIED_COHORT_VERSION
    journal = TradingJournal(tmp_path / "separate-cohorts.db", owner="synthetic-owner")
    raw = tracking.activate_model_experiment(journal, {"source": "growth"}, source="growth",
        now="2026-10-19T19:00:00Z")
    store = ModelResultsStore(journal)
    before = deepcopy(store.select("snapshots"))
    verified = tracking.activate_model_experiment(journal, {"source": "growth", "cohort": "verified",
        "cohort_version": VERIFIED_COHORT_VERSION}, source="growth", cohort="verified",
        now="2026-10-19T19:00:00Z")
    assert raw != verified and len(store.list_seasons()) == 16
    assert all(row in store.select("snapshots") for row in before)
    headers = [row for row in store.list_seasons() if row["manifest"]["experiment_id"] == verified]
    assert tracking.experiment_readiness(headers)["ready"]
    assert all(row["manifest"]["cohort"] == "verified"
               and row["manifest"]["cohort_version"] == VERIFIED_COHORT_VERSION for row in headers)


def test_multiple_uncollected_days_never_accumulate_future_buys(activated):
    journal, identifier, store = activated
    signals = pd.DataFrame([
        dict(id=1, ticker="AAA", analyzed_at="2026-10-19", created_at="2026-10-19T20:01:00Z",
             entry_label="Entrada fuerte", entry_score=90, price=100, note=""),
        dict(id=2, ticker="AAA", analyzed_at="2026-10-20", created_at="2026-10-20T20:01:00Z",
             entry_label="Entrada fuerte", entry_score=90, price=100, note=""),
    ])
    before = {kind: deepcopy(store.select(kind)) for kind in ("batches", "events", "snapshots")}
    with pytest.raises(tracking.ModelTrackingBacklog, match="no economic backfill"):
        tracking.advance_model_experiment(journal, identifier, signals, {}, now="2026-10-21T21:00:00Z")
    assert store.select("snapshots") == before["snapshots"]
    events = store.select("events")
    assert len(events) == 1 and events[0]["event_type"] == "incident"
    assert events[0]["payload"]["code"] == "collector_backlog_requires_new_season"
    after = {kind: deepcopy(store.select(kind)) for kind in before}
    with pytest.raises(tracking.ModelTrackingBacklog, match="remains paused"):
        tracking.advance_model_experiment(journal, identifier, signals.tail(1), {}, now="2026-10-22T21:00:00Z")
    assert after == {kind: store.select(kind) for kind in before}


def test_unknown_future_snapshots_cannot_create_a_backlog_incident_or_change_current_results(tmp_path):
    days = pd.bdate_range("2026-10-16", "2026-10-23")
    frame = pd.DataFrame(dict(open=100., high=101., low=99., close=100., fx_open=1., fx_close=1.,
        sma_long=90., reduce_setup=False, sell_setup=False), index=days)
    frame["open_at"] = pd.to_datetime([str(day.date()) + "T14:00:00Z" for day in days])
    frame["close_at"] = pd.to_datetime([str(day.date()) + "T20:00:00Z" for day in days])
    frame.attrs.update(display_currency="USD", required_last_close_at="2026-10-19T20:00:00Z",
        expected_sessions=["2026-10-16", "2026-10-19"],
        session_schedule=[dict(session=str(day.date()), open_at=row.open_at.isoformat(), close_at=row.close_at.isoformat())
            for day, row in frame.iterrows()])
    baseline = dict(id=1, ticker="AAA", analyzed_at="2026-10-16", created_at="2026-10-16T20:01:00Z",
                    entry_label="Esperar", entry_score=40, price=100, note="")
    current = {**baseline, "id": 2, "analyzed_at": "2026-10-19", "created_at": "2026-10-19T20:01:00Z",
               "entry_label": "Entrada fuerte", "entry_score": 90}
    future = [{**current, "id": index + 3, "analyzed_at": day, "created_at": day + "T20:01:00Z"}
              for index, day in enumerate(("2026-10-20", "2026-10-21"))]
    archives = []
    for name, extra in (("prefix", []), ("future", future)):
        journal = TradingJournal(tmp_path / (name + ".db"), owner="synthetic-owner")
        identifier = tracking.activate_model_experiment(journal, {"audit": "synthetic"}, now="2026-10-19T19:00:00Z")
        outcome = tracking.advance_model_experiment(journal, identifier, pd.DataFrame([baseline, current, *extra]),
            {"AAA": frame}, now="2026-10-19T21:00:00Z")
        store = ModelResultsStore(journal)
        assert outcome["runs"] == 8
        assert not any(row["payload"].get("code") == "collector_backlog_requires_new_season"
                       for row in store.select("events"))
        archives.append({kind: store.select(kind) for kind in ("events", "snapshots")})
    assert archives[0] == archives[1]


def test_disabled_flag_is_visible_without_opening_backend(monkeypatch, capsys):
    monkeypatch.setenv("MODEL_RESULTS_ENABLED", "")
    factory = Mock(side_effect=AssertionError("must stay inert"))
    monkeypatch.setattr(runner, "create_journal", factory)
    assert runner.main() == 0
    assert "::warning::" in capsys.readouterr().out
    factory.assert_not_called()


def test_discovery_failure_returns_failure_without_private_error_text(monkeypatch, capsys):
    monkeypatch.setenv("MODEL_RESULTS_ENABLED", " true ")
    backend = SimpleNamespace(url="https://private.invalid", _list_rows=Mock(
        side_effect=RuntimeError("private-owner@example.invalid secret-token")))
    monkeypatch.setattr(runner, "create_journal", lambda owner: backend)
    assert runner.main() == 1
    text = capsys.readouterr()
    assert "::error::" in text.err
    assert "private-owner" not in text.err and "secret-token" not in text.err


def test_enabled_without_activation_reports_unmeasured_not_successful_returns(monkeypatch, capsys):
    monkeypatch.setenv("MODEL_RESULTS_ENABLED", "true")
    backend = SimpleNamespace(url="https://example.invalid", _list_rows=Mock(return_value=[]))
    monkeypatch.setattr(runner, "create_journal", lambda owner: backend)
    assert runner.main() == 0
    assert "sin experimentos activados" in capsys.readouterr().out


def test_explicit_owner_deployment_has_kill_switch_and_recovery_does_not_send_mail():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/model-results.yml").read_text()
    assert "MODEL_RESULTS_ENABLED: ${{ vars.MODEL_RESULTS_ENABLED || 'true' }}" in workflow
    assert "MODEL_RESULTS_OWNER: ${{ github.repository_owner }}" in workflow
    assert "if: vars.MODEL_RESULTS_ENABLED == 'true'" not in workflow
    assert "EMAIL_SMTP" not in workflow
    assert "workflow_run:" not in workflow
    assert "group: stock-signal-lab-daily-alerts" in workflow


def test_warm_tracking_view_refreshes_missing_readiness_api_before_binding_exports():
    script = '''
import importlib
import sys
import src.model_results_tracking as tracking
del tracking.TRACKING_API_VERSION
del tracking.experiment_readiness
sys.modules.pop("src.model_results_tracking_ui", None)
ui = importlib.import_module("src.model_results_tracking_ui")
assert callable(ui.experiment_readiness)
assert tracking.TRACKING_API_VERSION == "measurement-readiness-v2"
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
        timeout=45, cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stdout + result.stderr


def test_warm_tracking_view_refreshes_old_cohort_normalizer_binding():
    script = '''
import importlib
import inspect
import sys
import src.strategy_audit_data as audit
import src.model_results_tracking as tracking
def legacy_normalizer(snapshots, source="technical"):
    raise AssertionError("old normalization must not run")
audit.normalize_audit_signals = legacy_normalizer
tracking.normalize_audit_signals = legacy_normalizer
sys.modules.pop("src.model_results_tracking_ui", None)
ui = importlib.import_module("src.model_results_tracking_ui")
assert "cohort" in inspect.signature(tracking.normalize_audit_signals).parameters
assert tracking.normalize_audit_signals is audit.normalize_audit_signals
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
        timeout=45, cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stdout + result.stderr
