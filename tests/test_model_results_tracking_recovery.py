"""A collector crash must never turn a replay-only sale into spendable cash."""

from copy import deepcopy

import pandas as pd
import pytest

from src.journal import TradingJournal
from src.model_results_engine import ExecutionCosts
from src.model_results_store import ModelResultsStore
import src.model_results_tracking as tracking


def _quotes(through):
    days = pd.bdate_range("2026-10-16", "2026-10-23")
    frame = pd.DataFrame(dict(open=100.0, high=101.0, low=99.0, close=100.0,
        fx_open=1.0, fx_close=1.0, sma_long=90.0, reduce_setup=False,
        sell_setup=[day.date().isoformat() == "2026-10-21" for day in days]), index=days)
    frame["open_at"] = pd.to_datetime([day.date().isoformat() + "T14:00:00Z" for day in days])
    frame["close_at"] = pd.to_datetime([day.date().isoformat() + "T20:00:00Z" for day in days])
    schedule = [dict(session=day.date().isoformat(), open_at=row.open_at.isoformat(),
        close_at=row.close_at.isoformat()) for day, row in frame.iterrows()]
    selected = frame.loc[:through].copy()
    selected.attrs.update(display_currency="USD", session_schedule=schedule,
        expected_sessions=[item["session"] for item in schedule if item["session"] <= through],
        calendar_coverage_start="2026-10-16", calendar_coverage_end="2026-10-23",
        required_last_close_at=selected.iloc[-1].close_at.isoformat())
    return {"AAA": selected}


def _signals(*, new_entry=False):
    records = [dict(id=1, ticker="AAA", analyzed_at="2026-10-16",
        created_at="2026-10-16T20:01:00Z", entry_label="Esperar", entry_score=50,
        price=100.0, note=""), dict(id=2, ticker="AAA", analyzed_at="2026-10-19",
        created_at="2026-10-19T20:01:00Z", entry_label="Entrada fuerte", entry_score=90,
        price=100.0, note="")]
    if new_entry:
        records.append(dict(id=3, ticker="AAA", analyzed_at="2026-10-22",
            created_at="2026-10-22T20:01:00Z", entry_label="Entrada fuerte", entry_score=90,
            price=100.0, note=""))
    return pd.DataFrame(records)


def test_crash_after_feed_before_sell_intention_never_recycles_unexecuted_sale_cash(tmp_path, monkeypatch):
    journal = TradingJournal(tmp_path / "synthetic-recovery.db", owner="synthetic-owner")
    experiment = tracking.activate_model_experiment(journal, {"audit": "synthetic-frozen"},
        now="2026-10-19T19:00:00Z", budget_usd=100.0, costs=ExecutionCosts(0, 0, 0))
    store = ModelResultsStore(journal)
    budget_a = next(row for row in store.list_seasons()
        if row["manifest"]["experiment_id"] == experiment
        and row["manifest"]["strategy_key"] == "A" and row["manifest"]["mode"] == "budget")
    season_id = budget_a["season_id"]

    # A real pre-open model intention and the first valid USD100 fill already
    # exist. The later incident may not delete or rewrite either fact.
    tracking.advance_model_experiment(journal, experiment, _signals(), _quotes("2026-10-19"),
        now="2026-10-19T21:00:00Z")
    tracking.advance_model_experiment(journal, experiment, _signals(), _quotes("2026-10-20"),
        now="2026-10-20T21:00:00Z")
    before = deepcopy(store.list_events(season_id))
    original_fills = [row for row in before if row["event_type"] == "order_filled"]
    assert len(original_fills) == 1
    assert original_fills[0]["payload"]["side"] == "buy"
    assert original_fills[0]["payload"]["gross_usd"] == 100.0
    assert original_fills[0]["occurred_at"].startswith("2026-10-20T14:00")

    # A non-atomic process crash commits the new close to the immutable shared
    # feed, but no season gets its sell proposal/snapshot transaction committed.
    append = tracking._append

    def fail_before_model_batch(store, season_id, **kwargs):
        if kwargs.get("snapshot") is not None:
            raise RuntimeError("synthetic crash after feed before model intentions")
        return append(store, season_id, **kwargs)

    monkeypatch.setattr(tracking, "_append", fail_before_model_batch)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        tracking.advance_model_experiment(journal, experiment, _signals(), _quotes("2026-10-21"),
            now="2026-10-21T21:00:00Z")
    monkeypatch.setattr(tracking, "_append", append)
    assert store.list_events(season_id) == before

    # On restart, replay would sell at Oct22 open without an archived sell
    # intention, then spend that fictitious USD100 on Oct23's new strong entry.
    # The complete season must now fail closed, not just hide its displayed NAV.
    tracking.advance_model_experiment(journal, experiment, _signals(new_entry=True),
        _quotes("2026-10-22"), now="2026-10-22T21:00:00Z")
    blocked_events = store.list_events(season_id)
    incidents = [row for row in blocked_events
        if row["payload"].get("code") == "prospective_execution_blocked"]
    assert len(incidents) == 1
    assert incidents[0]["payload"]["requires_new_season"] is True
    assert incidents[0]["payload"]["missing_preopen_intentions"]
    assert [row for row in blocked_events if row["event_type"] == "order_filled"] == original_fills
    assert not any(row["event_type"] in {"order_filled", "order_proposed", "decision"}
        for row in blocked_events[len(before):])
    last = store.list_snapshots(season_id)[-1]["snapshot"]
    assert last["metrics"] is None and last["coverage_complete"] is False
    assert last["replay_diagnostics"] is not None

    # A later process/restart must preserve the permanent stop, even though the
    # replay's cash ledger could afford another purchase with the missing sale.
    tracking.advance_model_experiment(journal, experiment, _signals(new_entry=True),
        _quotes("2026-10-23"), now="2026-10-23T21:00:00Z")
    final_events = store.list_events(season_id)
    assert [row for row in final_events if row["event_type"] == "order_filled"] == original_fills
    assert not any(row["event_type"] in {"order_filled", "order_proposed", "decision"}
        for row in final_events[len(before):])
    assert sum(row["payload"].get("code") == "prospective_execution_blocked" for row in final_events) == 1
    assert store.list_snapshots(season_id)[-1]["snapshot"]["metrics"] is None
    assert budget_a["manifest"]["initial_cash_usd"] == 100.0
    assert journal.list_operations().empty and journal.list_paper_simulations().empty
