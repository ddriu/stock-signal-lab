"""Offline end-to-end replay/knowledge-clock tests; no SMTP or market access."""
from copy import deepcopy
from dataclasses import replace

import pandas as pd
import pytest

from src.journal import TradingJournal
from src.model_results_store import ModelResultsStore, ModelResultsConflict
from src.model_results_tracking import (activate_model_experiment, advance_model_experiment,
    engine_identity, record_model_email_evidence, resume_model_activation)


def quotes(through="2026-10-19", *, sell_day=None, prices=None):
    days = pd.bdate_range("2026-10-16", "2026-10-23")
    prices = prices or [100] * len(days)
    frame = pd.DataFrame(dict(open=prices, close=prices, high=[p + 1 for p in prices],
        low=[p - 1 for p in prices], fx_open=1., fx_close=1., sma_long=90.,
        reduce_setup=False, sell_setup=[str(d.date()) == sell_day for d in days]), index=days)
    frame["open_at"] = pd.to_datetime([str(d.date()) + "T14:00:00Z" for d in days])
    frame["close_at"] = pd.to_datetime([str(d.date()) + "T20:00:00Z" for d in days])
    schedule = [dict(session=str(d.date()), open_at=r.open_at.isoformat(), close_at=r.close_at.isoformat())
                for d, r in frame.iterrows()]
    out = frame.loc[:through].copy()
    out.attrs.update(display_currency="USD", session_schedule=schedule,
        expected_sessions=[s["session"] for s in schedule if s["session"] <= through],
        required_last_close_at=out.iloc[-1].close_at.isoformat())
    return {"AAA": out}


def signals(extra=()):
    baseline = dict(id=1, ticker="AAA", analyzed_at="2026-10-16", created_at="2026-10-16T20:01:00Z",
        entry_label="Esperar", entry_score=50, price=100, note="")
    strong = {**baseline, "id": 2, "analyzed_at": "2026-10-19", "created_at": "2026-10-19T20:01:00Z",
        "entry_label": "Entrada fuerte", "entry_score": 90}
    return pd.DataFrame([baseline, strong, *extra])


@pytest.fixture
def journal(tmp_path):
    return TradingJournal(tmp_path / "model.db", owner="test-owner")


def activate(journal):
    return activate_model_experiment(journal, {"input_hash": "frozen-audit", "coverage_complete": False},
        now="2026-10-19T19:00:00Z")


def season(store, experiment, model="A", mode="theoretical"):
    return next(r for r in store.list_seasons() if r["manifest"]["experiment_id"] == experiment
        and r["manifest"]["strategy_key"] == model and r["manifest"]["mode"] == mode)


def test_requires_retrospective_evidence_and_timezone(journal):
    with pytest.raises(ValueError, match="retrospective"):
        activate_model_experiment(journal, {})
    with pytest.raises(ValueError, match="aware"):
        activate_model_experiment(journal, {"done": True}, now="2026-10-19")


def test_activation_only_creates_virtual_usd_headers(journal):
    experiment = activate(journal)
    store = ModelResultsStore(journal)
    assert len(store.list_seasons()) == 8
    assert all(r["paper_simulation_id"] is None for r in store.list_seasons())
    assert journal.list_paper_simulations().empty
    assert journal.list_operations().empty
    assert season(store, experiment)["manifest"]["initial_cash_usd"] == 0
    assert season(store, experiment)["manifest"]["initial_nav_usd"] == 0


def test_partial_activation_is_resumable_after_session_restart(journal, monkeypatch):
    original = ModelResultsStore.create_season
    calls = []
    def interrupted(self, config, **kwargs):
        calls.append(config)
        if len(calls) == 4:
            raise RuntimeError("simulated RPC interruption")
        return original(self, config, **kwargs)
    monkeypatch.setattr(ModelResultsStore, "create_season", interrupted)
    with pytest.raises(RuntimeError):
        activate(journal)
    monkeypatch.setattr(ModelResultsStore, "create_season", original)
    store = ModelResultsStore(journal)
    before = store.list_seasons()
    assert len(before) == 3
    experiment = before[0]["manifest"]["experiment_id"]
    assert resume_model_activation(journal, experiment) == experiment
    assert len(store.list_seasons()) == 8
    assert all(row in store.list_seasons() for row in before)
    resume_model_activation(journal, experiment)
    assert len(store.list_seasons()) == 8


def test_entry_proposed_before_open_and_fill_after_signal_not_same_close(journal):
    experiment = activate(journal)
    first = advance_model_experiment(journal, experiment, signals(), quotes(), now="2026-10-19T21:00:00Z")
    store = ModelResultsStore(journal)
    sid = season(store, experiment)["season_id"]
    events = store.list_events(sid)
    assert first["runs"] == 8
    assert not any(r["event_type"] == "order_filled" for r in events)
    assert any(r["event_type"] == "order_proposed" for r in events)
    advance_model_experiment(journal, experiment, signals(), quotes("2026-10-20"), now="2026-10-20T21:00:00Z")
    events = store.list_events(sid)
    fill = next(r for r in events if r["event_type"] == "order_filled")
    assert fill["occurred_at"].startswith("2026-10-20T14:00")
    assert fill["available_at"].startswith("2026-10-20T21:00")
    assert fill["payload"]["gross_usd"] == 100
    assert fill["payload"]["is_real_execution"] is False


def test_restart_same_inputs_produces_no_events_or_snapshots(journal):
    experiment = activate(journal)
    advance_model_experiment(journal, experiment, signals(), quotes(), now="2026-10-19T21:00:00Z")
    store = ModelResultsStore(journal)
    before = {kind: store.select(kind) for kind in ("events", "snapshots", "batches")}
    result = advance_model_experiment(journal, experiment, signals(), quotes(), now="2026-10-19T21:01:00Z")
    assert result["runs"] == result["new_inputs"] == 0
    assert before == {kind: store.select(kind) for kind in before}


def test_late_signal_never_buys_a_past_open(journal):
    experiment = activate(journal)
    advance_model_experiment(journal, experiment, signals(), quotes("2026-10-20"), now="2026-10-20T21:00:00Z")
    store = ModelResultsStore(journal)
    sid = season(store, experiment)["season_id"]
    assert not any(r["event_type"] == "order_filled" for r in store.list_events(sid))
    advance_model_experiment(journal, experiment, signals(), quotes("2026-10-21"), now="2026-10-21T21:00:00Z")
    fill = next(r for r in store.list_events(sid) if r["event_type"] == "order_filled")
    assert fill["occurred_at"].startswith("2026-10-21T14:00")


def test_exit_observed_late_moves_to_later_open_not_backdated(journal):
    experiment = activate(journal)
    advance_model_experiment(journal, experiment, signals(), quotes(), now="2026-10-19T21:00:00Z")
    advance_model_experiment(journal, experiment, signals(), quotes("2026-10-20", sell_day="2026-10-20"), now="2026-10-21T21:00:00Z")
    store = ModelResultsStore(journal)
    sid = season(store, experiment)["season_id"]
    assert not any(r["payload"].get("side") == "sell" and r["event_type"] == "order_filled" for r in store.list_events(sid))
    advance_model_experiment(journal, experiment, signals(), quotes("2026-10-22", sell_day="2026-10-20"), now="2026-10-22T21:00:00Z")
    sales = [r for r in store.list_events(sid) if r["payload"].get("side") == "sell" and r["event_type"] == "order_filled"]
    assert len(sales) == 1
    assert sales[0]["occurred_at"].startswith("2026-10-22T14:00")


def test_price_revision_never_rewrites_a_fill_or_claims_reliable_performance(journal):
    experiment = activate(journal)
    advance_model_experiment(journal, experiment, signals(), quotes(), now="2026-10-19T21:00:00Z")
    advance_model_experiment(journal, experiment, signals(), quotes("2026-10-20"), now="2026-10-20T21:00:00Z")
    revised = quotes("2026-10-21", prices=[100, 100, 500, 100, 100, 100])
    advance_model_experiment(journal, experiment, signals(), revised, now="2026-10-21T21:00:00Z")
    store = ModelResultsStore(journal)
    sid = season(store, experiment)["season_id"]
    assert len([r for r in store.list_events(sid) if r["event_type"] == "order_filled"]) == 1
    last = store.list_snapshots(sid)[-1]["snapshot"]
    assert last["coverage_complete"] is False
    assert last["metrics"] is None
    assert last["provider_revision_detected"] is True


def test_config_or_code_changes_require_separate_season(journal, monkeypatch):
    experiment = activate(journal)
    monkeypatch.setattr("src.model_results_tracking.engine_identity", lambda: {"signature": "different"})
    with pytest.raises(ModelResultsConflict, match="Frozen engine"):
        advance_model_experiment(journal, experiment, signals(), quotes(), now="2026-10-19T21:00:00Z")


def test_email_hook_disabled_by_default_never_creates_ledger(journal, monkeypatch):
    monkeypatch.delenv("MODEL_RESULTS_ENABLED", raising=False)
    record_model_email_evidence(journal, None, "test")
    with journal._connect() as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='model_result_seasons'").fetchone() is None
