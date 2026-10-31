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
    headers = [{"manifest": {"experiment_id": name, "engine_version": TRACKING_VERSION},
                "season_id": name + str(i)}
               for name, size in (("partial", 1), ("complete", 8)) for i in range(size)]
    monkeypatch.setattr(runner, "create_journal", lambda owner:
        backend if owner == runner.GROUP_PORTFOLIO_OWNER else journal)
    monkeypatch.setattr(runner, "create_model_results_store", lambda journal:
        SimpleNamespace(list_seasons=lambda: headers, list_events=lambda season: []))
    monkeypatch.setattr(runner, "experiment_readiness", lambda rows: dict(
        ready=len(rows) == 8, status="ready" if len(rows) == 8 else "partial_activation",
        requires_new_experiment=False))
    calls = []
    def advance(journal, store, seasons, snapshots, experiment, manifest, **kwargs):
        calls.append(experiment)
        if experiment == "partial":
            raise ValueError("private diagnostic")
        return {"runs": 8}
    monkeypatch.setattr(runner, "advance_one", advance)
    assert runner.main() == 1  # Fault remains visible, never a silent success.
    assert calls == ["complete"]  # Incomplete is diagnosed before download/advance.
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


def _headers(name, source="growth", cohort="verified"):
    return [dict(season_id=f"{name}-{mode}-{strategy}", manifest=dict(
        experiment_id=name, engine_version=TRACKING_VERSION, source=source,
        cohort=cohort, mode=mode, strategy_key=strategy, budget_usd=6000))
        for mode in ("theoretical", "budget") for strategy in "ABCD"]


def test_explicit_collector_uses_supplied_snapshots_skips_preserved_legacy_and_ignores_global_switch(monkeypatch):
    monkeypatch.delenv("MODEL_RESULTS_ENABLED", raising=False)
    snapshots = pd.DataFrame([{"ticker": "synthetic"}])
    journal = SimpleNamespace(list_analysis_snapshots=Mock(side_effect=AssertionError("must reuse source")))
    headers = _headers("legacy", source="technical", cohort="raw") + _headers("current")
    store = SimpleNamespace(list_seasons=lambda: headers, list_events=lambda season: [])
    monkeypatch.setattr(runner, "create_model_results_store", lambda value: store)
    monkeypatch.setattr(runner, "experiment_readiness", lambda rows: dict(
        ready=rows[0]["manifest"]["experiment_id"] == "current", status="frozen_engine_changed",
        requires_new_experiment=rows[0]["manifest"]["experiment_id"] == "legacy"))
    advance = Mock(return_value={"runs": 8, "status": "updated", "new_inputs": 1})
    monkeypatch.setattr(runner, "advance_one", advance)
    result = runner.collect_owner_models(journal, snapshots, now="2026-10-31T11:00:00Z")
    assert result["status"] == "updated" and result["issues"] == 0
    assert result["ready_experiments"] == result["preserved_experiments"] == 1
    assert result["runs"] == 8 and result["new_fills"] == result["new_proposals"] == 0
    assert result["available_at"] == "2026-10-31T11:00:00+00:00"
    assert result["is_real_execution"] is False
    assert advance.call_args.args[3] is snapshots
    assert advance.call_args.args[4] == "current"


def test_collector_counts_new_ledger_orders_not_season_updates_and_restart_is_unchanged(monkeypatch):
    headers = _headers("current")
    first = headers[0]["season_id"]
    events = {row["season_id"]: [] for row in headers}
    events[first] = [dict(event_id="old", event_type="order_proposed", payload=dict(
        intended_open_at="2026-10-30T13:30:00Z"))]
    store = SimpleNamespace(list_seasons=lambda: headers, list_events=lambda season: list(events[season]))
    monkeypatch.setattr(runner, "create_model_results_store", lambda value: store)
    monkeypatch.setattr(runner, "experiment_readiness", lambda rows: dict(ready=True))
    def advance(*args, **kwargs):
        if len(events[first]) > 1:
            return {"runs": 0, "status": "unchanged"}
        events[first] += [
            dict(event_id="fill", event_type="order_filled", payload={"intention_id": "old"}),
            dict(event_id="future", event_type="order_proposed", payload=dict(
                intended_open_at="2026-11-02T14:30:00Z")),
            dict(event_id="incident", event_type="incident", payload={}),
        ]
        return {"runs": 8, "status": "updated"}
    monkeypatch.setattr(runner, "advance_one", advance)
    result = runner.collect_owner_models(object(), pd.DataFrame(), now="2026-10-31T11:00:00Z")
    assert result["new_fills"] == result["new_proposals"] == result["pending_proposals"] == 1
    assert result["cumulative_proposals"] == 2 and result["cumulative_fills"] == 1
    assert result["ledger_incidents"] == 1 and result["runs"] == 8
    repeated = runner.collect_owner_models(object(), pd.DataFrame(), now="2026-10-31T11:00:00Z")
    assert repeated["status"] == "unchanged" and repeated["new_proposals"] == repeated["new_fills"] == 0
    assert repeated["pending_proposals"] == 1


def test_failed_event_read_does_not_claim_zero_fills_or_leak_private_exception(monkeypatch):
    headers = _headers("current")
    store = SimpleNamespace(list_seasons=lambda: headers,
        list_events=Mock(side_effect=RuntimeError("private-user https://private.invalid")))
    monkeypatch.setattr(runner, "create_model_results_store", lambda value: store)
    monkeypatch.setattr(runner, "experiment_readiness", lambda rows: dict(ready=True))
    advance = Mock(side_effect=AssertionError("cannot safely advance"))
    monkeypatch.setattr(runner, "advance_one", advance)
    result = runner.collect_owner_models(object(), pd.DataFrame(), now="2026-10-31T11:00:00Z")
    assert result["status"] == "error" and result["issues"] > 0
    assert result["new_fills"] is result["new_proposals"] is result["pending_proposals"] is None
    assert "private-user" not in str(result) and "https://" not in str(result)
    advance.assert_not_called()


def test_scheduler_owner_filter_is_applied_server_side_and_locally(monkeypatch):
    monkeypatch.setenv("MODEL_RESULTS_ENABLED", "true")
    monkeypatch.setenv("MODEL_RESULTS_OWNER", "allowed-user")
    backend = SimpleNamespace(url="https://example.invalid", _list_rows=Mock(return_value=[
        {"owner": "other-user", "season_id": "other"}, {"owner": "allowed-user", "season_id": "allowed"}]))
    owner_journal = object()
    factory = Mock(side_effect=lambda owner: backend if owner == runner.GROUP_PORTFOLIO_OWNER else owner_journal)
    monkeypatch.setattr(runner, "create_journal", factory)
    collect = Mock(return_value={"runs": 0, "issues": 0, "preserved_experiments": 0, "experiments": []})
    monkeypatch.setattr(runner, "collect_owner_models", collect)
    assert runner.main() == 0
    assert backend._list_rows.call_args.kwargs["params"]["owner"] == "eq.allowed-user"
    assert [call.args[0] for call in factory.call_args_list] == [runner.GROUP_PORTFOLIO_OWNER, "allowed-user"]
    collect.assert_called_once_with(owner_journal)


def test_experiment_filter_keeps_manual_review_on_one_frozen_activation(monkeypatch):
    headers = _headers("current") + _headers("other")
    store = SimpleNamespace(list_seasons=lambda: headers, list_events=lambda season: [])
    monkeypatch.setattr(runner, "create_model_results_store", lambda value: store)
    monkeypatch.setattr(runner, "experiment_readiness", lambda rows: dict(ready=True))
    advance = Mock(return_value={"runs": 0, "status": "waiting_for_signals"})
    monkeypatch.setattr(runner, "advance_one", advance)
    result = runner.collect_owner_models(object(), pd.DataFrame(), experiment_id="current",
        now="2026-10-31T11:00:00Z")
    assert result["status"] == "waiting_for_signals" and len(result["experiments"]) == 1
    assert result["experiments"][0]["experiment_id"] == "current"
    advance.assert_called_once()


def test_operation_preview_is_bounded_allowlisted_and_never_invents_a_fill_price(monkeypatch):
    headers = _headers("current")
    events = {header["season_id"]: [] for header in headers}
    store = SimpleNamespace(list_seasons=lambda: headers, list_events=lambda season: list(events[season]))
    monkeypatch.setattr(runner, "create_model_results_store", lambda value: store)
    monkeypatch.setattr(runner, "experiment_readiness", lambda rows: dict(ready=True))
    def advance(*args, **kwargs):
        for header in headers:
            events[header["season_id"]] = [dict(event_id="proposal", event_type="order_proposed", payload=dict(
                ticker="AAA", side="buy", intended_open_at="2026-11-02T14:30:00Z", notional_usd=100.,
                recipient="private@example.invalid", credentials="never copied"))]
        return {"runs": 8, "status": "updated"}
    monkeypatch.setattr(runner, "advance_one", advance)
    result = runner.collect_owner_models(object(), pd.DataFrame(), now="2026-10-31T11:00:00Z")
    assert result["new_proposals"] == result["pending_proposals"] == 8 and result["new_fills"] == 0
    assert len(result["operations"]) == 5 and result["operations_preview_truncated"]
    for operation in result["operations"]:
        assert operation["ticker"] == "AAA" and operation["notional_usd"] == 100.
        assert operation["event_type"] == "order_proposed" and operation["is_real_execution"] is False
        assert "price_native" not in operation and "executed_at" not in operation
    assert "private@example" not in str(result) and "credentials" not in str(result)


def test_live_collector_does_not_backdate_tracker_clock_to_before_download(monkeypatch):
    headers = _headers("current")
    store = SimpleNamespace(list_seasons=lambda: headers, list_events=lambda season: [])
    monkeypatch.setattr(runner, "create_model_results_store", lambda value: store)
    monkeypatch.setattr(runner, "experiment_readiness", lambda rows: dict(ready=True))
    advance = Mock(return_value={"runs": 0, "status": "unchanged"})
    monkeypatch.setattr(runner, "advance_one", advance)
    runner.collect_owner_models(object(), pd.DataFrame())
    assert advance.call_args.kwargs["now"] is None


def test_warmup_prices_are_not_archived_but_indicators_and_future_calendar_are_preserved(monkeypatch):
    from src.model_results_tracking import _bar_rows
    days = pd.bdate_range("2025-08-01", "2026-11-02")
    frame = pd.DataFrame(dict(open=100., high=101., low=99., close=100., sma_long=900.), index=days)
    frame["close_at"] = pd.to_datetime([str(day.date()) + "T20:00:00Z" for day in days])
    frame.attrs.update(display_currency="USD", session_schedule=[
        dict(session="2026-10-30", open_at="2026-10-30T13:30:00Z", close_at="2026-10-30T20:00:00Z"),
        dict(session="2026-11-02", open_at="2026-11-02T14:30:00Z", close_at="2026-11-02T21:00:00Z")],
        source_provider="synthetic", expected_sessions=["2026-10-30"])
    headers = _headers("current")
    manifest = dict(headers[0]["manifest"], start_date="2026-10-31", activation_at="2026-10-31T10:00:00Z")
    store = SimpleNamespace(list_events=lambda season: [])
    monkeypatch.setattr(runner, "experiment_readiness", lambda rows: dict(ready=True))
    monkeypatch.setattr(runner, "collect_model_market_bundle", Mock(return_value=SimpleNamespace(bars={"AAA": frame})))
    advance = Mock(return_value={"runs": 0, "status": "waiting_for_signals"})
    monkeypatch.setattr(runner, "advance_model_experiment", advance)
    runner.advance_one(object(), store, headers, pd.DataFrame(), "current", manifest,
        now="2026-10-31T11:00:00Z")
    trimmed = advance.call_args.args[3]["AAA"]
    assert list(trimmed.index.date.astype(str)) == ["2026-10-30", "2026-11-02"]
    assert trimmed.attrs == frame.attrs and trimmed.attrs is not frame.attrs
    assert (trimmed.sma_long == 900.).all()  # Never recalculate on shortened data.
    assert len(frame) > 300  # Source bundle was not mutated.
    archived = list(_bar_rows({"AAA": trimmed}, pd.Timestamp("2026-10-31T11:00:00Z")))
    assert len(archived) == 1 and archived[0][1]["session"] == "2026-10-30"
    assert trimmed.attrs["session_schedule"][-1]["open_at"] == "2026-11-02T14:30:00Z"


def test_activation_during_a_session_retains_that_close_and_only_one_prior_price_anchor():
    days = pd.date_range("2026-10-27", "2026-10-30")
    frame = pd.DataFrame(dict(close_at=pd.to_datetime([
        str(day.date()) + "T20:00:00Z" for day in days])), index=days)
    trimmed = runner._prospective_prices({"AAA": frame}, "2026-10-30T17:00:00Z")["AAA"]
    assert list(trimmed.index.date.astype(str)) == ["2026-10-29", "2026-10-30"]
