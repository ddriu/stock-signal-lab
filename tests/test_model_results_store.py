"""Offline safety/idempotency contract for the original-model daily ledger."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.journal import TradingJournal
from src.model_results_store import (
    GENESIS_HASH, ModelResultsConflict, ModelResultsStore,
    SupabaseModelResultsStore, content_hash, create_model_results_store, deterministic_event_id,
)


def manifest(**overrides):
    return {
        "season_key": "daily-2026", "strategy_key": "A", "mode": "budget",
        "start_date": "2026-10-20", "base_currency": "USD", "initial_nav_usd": 10000,
        "config": {"budget_usd": 10000, "max_holdings": 10},
        "git_commit": "c" * 40, "engine_version": "model-original-v1",
        **overrides,
    }


def event(event_id="decision-1", event_type="decision", at="2026-10-20T20:00:00+00:00", **overrides):
    return {
        "event_id": event_id, "event_type": event_type, "occurred_at": at,
        "available_at": at, "payload": {"action": "hold", "ticker": "AAPL"},
        **overrides,
    }


def snapshot(**overrides):
    return {
        "available_at": "2026-10-20T20:00:00+00:00", "base_currency": "USD",
        "cash_usd": 10000, "net_nav_usd": 10000, "benchmark_nav_usd": None,
        "positions": [], "counts": {"candidates": 0, "filled": 0},
        "quality_flags": ["benchmark_missing"], "context": {"git_commit": "c" * 40},
        **overrides,
    }


@pytest.fixture
def journal(tmp_path):
    return TradingJournal(tmp_path / "journal.db", owner="owner-a")


@pytest.fixture
def store(journal):
    return ModelResultsStore(journal)


def append(store, season, **overrides):
    return store.append_batch(season["season_id"], **{
        "run_id": "2026-10-20", "market_date": "2026-10-20", "events": [event()],
        "snapshot": snapshot(), "expected_previous_hash": GENESIS_HASH, **overrides,
    })


def test_usd_header_snapshot_does_not_forge_eur_or_touch_real_holdings(store, journal):
    journal.add_operation("REAL", "Compra", 1, 100, 0, "2026-10-01")
    before = journal.list_operations().to_dict("records")
    season = store.create_season(manifest())
    result = append(store, season)
    assert season["paper_simulation_id"] is None
    assert season["manifest"]["initial_nav_usd"] == 10000
    assert result["paper_daily_run_id"] is None
    assert journal.list_paper_simulations().empty
    assert journal.list_paper_daily_runs().empty
    assert journal.list_operations().to_dict("records") == before
    saved = store.list_snapshots(season["season_id"])[0]["snapshot"]
    assert saved["benchmark_nav_usd"] is None
    assert saved["counts"] == {"candidates": 0, "filled": 0}
    assert "net_nav_eur" not in saved


def test_season_is_idempotent_and_manifest_is_immutable(store):
    first = store.create_season(manifest())
    assert first == store.create_season(manifest())
    with pytest.raises(ModelResultsConflict, match="manifest"):
        store.create_season(manifest(initial_nav_usd=20000))
    assert len(store.list_seasons()) == 1


def test_external_entry_theory_has_real_zero_initial_nav_without_paper_sentinel(store, journal):
    season = store.create_season(manifest(mode="theoretical", initial_nav_usd=0,
        initial_cash_usd=0, funding_rule="external_100_per_entry", initial_positions=[]))
    assert season["manifest"]["initial_nav_usd"] == 0
    assert season["manifest"]["initial_cash_usd"] == 0
    assert season["paper_simulation_id"] is None
    assert journal.list_paper_simulations().empty


@pytest.mark.parametrize("overrides", [
    {"mode": "budget", "initial_nav_usd": 0, "funding_rule": "external_100_per_entry"},
    {"mode": "theoretical", "initial_nav_usd": 0},
    {"mode": "theoretical", "initial_nav_usd": -1, "funding_rule": "external_100_per_entry"},
    {"mode": "theoretical", "initial_nav_usd": float("inf"), "funding_rule": "external_100_per_entry"},
    {"mode": "theoretical", "initial_nav_usd": 0, "funding_rule": "external_100_per_entry", "initial_cash_usd": 100},
    {"mode": "theoretical", "initial_nav_usd": 0, "funding_rule": "external_100_per_entry", "initial_positions": [{"ticker": "AAPL"}]},
    {"mode": "theoretical", "base_currency": "EUR", "initial_nav_eur": 0, "funding_rule": "external_100_per_entry"},
])
def test_zero_nav_is_only_allowed_for_empty_usd_external_entry_theory(store, overrides):
    with pytest.raises(ValueError):
        store.create_season(manifest(**overrides))
    assert not store.list_seasons()


def test_all_four_strategies_and_modes_are_isolated(store):
    seasons = [store.create_season(manifest(strategy_key=strategy, mode=mode))
               for strategy in "ABCD" for mode in ("budget", "theoretical")]
    assert len({season["season_id"] for season in seasons}) == 8
    for season in seasons:
        append(store, season)
        assert len(store.list_events(season["season_id"])) == 1
    assert len(store.select("batches")) == 8


def test_exact_rerun_is_noop_even_with_old_expected_head(store):
    season = store.create_season(manifest())
    first = append(store, season)
    again = append(store, season)
    assert again["no_op"] is True
    assert again["head_hash"] == first["head_hash"]
    assert len(store.list_events(season["season_id"])) == 1
    assert len(store.select("batches")) == 1


@pytest.mark.parametrize("change", ["batch", "event", "snapshot"])
def test_conflicting_immutable_contents_are_rejected_without_partial_state(store, change):
    season = store.create_season(manifest())
    append(store, season, batch_id="daily-close")
    kwargs = {"batch_id": "other-batch", "expected_previous_hash": None}
    if change == "batch":
        kwargs.update(batch_id="daily-close", snapshot=snapshot(net_nav_usd=10001))
    elif change == "event":
        kwargs.update(events=[event(payload={"action": "buy"})])
    else:
        kwargs.update(events=[event("new-event")], snapshot=snapshot(net_nav_usd=10001))
    with pytest.raises(ModelResultsConflict):
        append(store, season, **kwargs)
    assert len(store.list_events(season["season_id"])) == 1
    assert len(store.list_snapshots(season["season_id"])) == 1
    assert len(store.select("batches")) == 1


def test_compare_and_swap_prevents_lost_update(store):
    season = store.create_season(manifest())
    first = append(store, season)
    with pytest.raises(ModelResultsConflict, match="Stale"):
        append(store, season, events=[event("event-2")], snapshot=None)
    second = append(store, season, events=[event("event-2")], snapshot=None,
                    expected_previous_hash=first["head_hash"])
    assert second["previous_hash"] == first["head_hash"]
    assert second["head_hash"] != first["head_hash"]


def test_two_concurrent_writers_commit_only_one_cas(journal, store):
    season = store.create_season(manifest())
    stores = [ModelResultsStore(journal), ModelResultsStore(journal)]

    def attempt(index):
        try:
            return append(stores[index], season, events=[event(f"event-{index}")], snapshot=None)
        except ModelResultsConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(store.list_events(season["season_id"])) == 1


@pytest.mark.parametrize("field,value", [
    ("occurred_at", "2026-10-20T20:00:00"),
    ("available_at", "2026-10-20T20:00:00"),
    ("occurred_at", "2026-10-20T20:00:01Z"),
])
def test_event_clocks_require_timezones_and_causality(store, field, value):
    season = store.create_season(manifest())
    with pytest.raises(ValueError):
        append(store, season, events=[event(**{field: value})])
    assert not store.select("batches")


def test_availability_is_monotonic_and_equivalent_offsets_are_canonical(store):
    season = store.create_season(manifest())
    first = append(store, season)
    same = event(at="2026-10-20T22:00:00+02:00")
    assert append(store, season, events=[same])["no_op"]
    with pytest.raises(ModelResultsConflict, match="backwards"):
        append(store, season, events=[event("late-fact", at="2026-10-20T19:59:59Z")],
               snapshot=None, expected_previous_hash=first["head_hash"])


def test_snapshot_requires_aware_available_at(store):
    season = store.create_season(manifest())
    with pytest.raises(ValueError, match="timezone"):
        append(store, season, snapshot=snapshot(available_at="2026-10-20T20:00:00"))


def test_owner_cannot_read_or_append_another_owners_data(journal, store):
    season = store.create_season(manifest())
    append(store, season)
    other = ModelResultsStore(TradingJournal(journal.database_path, owner="owner-b"))
    assert other.list_events(season["season_id"]) == []
    assert other.list_seasons() == []
    with pytest.raises(ValueError, match="owner"):
        append(other, season)


@pytest.mark.parametrize("owner", ["", "anon", "anonymous", "public"])
def test_no_anonymous_owner(journal, owner):
    journal.owner = owner
    with pytest.raises(ValueError, match="owner"):
        ModelResultsStore(journal)


@pytest.mark.parametrize("payload", [{"smtp_password": "secret"}, {"recipient": "redacted"}, {"message": "user@example.org"}])
def test_credentials_and_pii_are_not_saved(store, payload):
    season = store.create_season(manifest())
    with pytest.raises(ValueError):
        append(store, season, events=[event(payload=payload)])
    assert not store.select("events")


def test_snapshot_email_flags_never_create_delivery_records(store):
    season = store.create_season(manifest())
    append(store, season, snapshot=snapshot(email_delivered=True))
    assert all(row["event_type"] != "email_delivery" for row in store.list_events(season["season_id"]))


def test_smtp_acceptance_is_separate_from_the_daily_snapshot(store):
    season = store.create_season(manifest())
    first = append(store, season)
    payload = {"delivery_id": "delivery-1", "status": "smtp_accepted", "smtp_code": 250,
               "receipt_hash": "a" * 64, "accepted_at": "2026-10-20T20:01:00Z"}
    result = append(store, season, snapshot=None, events=[event("delivery-1", "email_delivery",
                    at="2026-10-20T20:01:00Z", payload=payload)],
                    expected_previous_hash=first["head_hash"])
    assert result["inserted_events"] == 1
    assert len(store.list_snapshots(season["season_id"])) == 1
    assert store.list_events(season["season_id"])[1]["payload"]["status"] == "smtp_accepted"


@pytest.mark.parametrize("payload", [
    {"delivery_id": "1", "status": "delivered"},
    {"delivery_id": "1", "status": "smtp_accepted", "smtp_code": 250},
    {"delivery_id": "1", "status": "smtp_accepted", "smtp_code": 550, "receipt_hash": "a" * 64},
])
def test_email_delivery_requires_actual_smtp_acceptance_proof(store, payload):
    season = store.create_season(manifest())
    with pytest.raises(ValueError):
        append(store, season, events=[event("mail", "email_delivery", payload=payload)])


def test_raw_sql_cannot_mutate_or_delete_ledger(store, journal):
    season = store.create_season(manifest())
    append(store, season)
    for table in ("model_result_seasons", "model_result_batches", "model_result_events", "model_result_snapshots"):
        for query in (f"DELETE FROM {table}", f"UPDATE {table} SET owner=owner"):
            with sqlite3.connect(journal.database_path) as connection:
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    connection.execute(query)


def test_crash_during_event_insert_rolls_back_entire_batch(store, journal):
    season = store.create_season(manifest())
    with sqlite3.connect(journal.database_path) as connection:
        connection.execute("""CREATE TRIGGER simulated_crash BEFORE INSERT ON model_result_events
           WHEN NEW.event_id='crash' BEGIN SELECT RAISE(ABORT,'simulated crash'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="simulated crash"):
        append(store, season, events=[event("first"), event("crash")])
    assert not store.select("events")
    assert not store.select("batches")
    assert not store.select("snapshots")


def paper_snapshot():
    return {"signal_as_of": "2026-10-20T20:00:00Z", "input_hash": "input-1",
            "positions_after": [], "cash_eur": 10000, "net_nav_eur": 10000,
            "hold_nav_eur": 10000, "benchmark_nav_eur": 10000}


def test_explicit_eur_projection_is_atomic_and_immutable_but_lab_is_unchanged(journal, store):
    lab_id = journal.create_paper_simulation(name="Old lab", start_date="2026-01-01", initial_nav_eur=1000, initial_positions=[])
    season = store.create_season(manifest(base_currency="EUR", initial_nav_eur=10000,
        paper_header={"initial_nav_eur": 10000, "initial_cash_eur": 10000}))
    first = append(store, season, paper_snapshot=paper_snapshot())
    assert first["paper_daily_run_id"] is not None
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        journal.upsert_paper_daily_run(simulation_id=season["paper_simulation_id"], market_date="2026-10-20", **paper_snapshot())
    # Existing laboratory upserts and status transitions retain their old contract.
    journal.set_paper_simulation_status(lab_id, "paused")
    journal.upsert_paper_daily_run(simulation_id=lab_id, market_date="2026-10-20", **paper_snapshot())
    journal.upsert_paper_daily_run(simulation_id=lab_id, market_date="2026-10-20", **paper_snapshot())
    assert len(journal.list_paper_daily_runs(lab_id)) == 1


def test_paper_projection_and_model_events_rollback_together(journal, store):
    season = store.create_season(manifest(base_currency="EUR", initial_nav_eur=10000,
        paper_header={"initial_nav_eur": 10000, "initial_cash_eur": 10000}))
    with sqlite3.connect(journal.database_path) as connection:
        connection.execute("""CREATE TRIGGER simulated_crash BEFORE INSERT ON model_result_events
           BEGIN SELECT RAISE(ABORT,'simulated crash'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        append(store, season, paper_snapshot=paper_snapshot())
    assert journal.list_paper_daily_runs(season["paper_simulation_id"]).empty
    assert not store.select("batches")


def test_usd_projection_without_fx_observation_is_rejected(store):
    with pytest.raises(ValueError, match="observed FX"):
        store.create_season(manifest(paper_header={"initial_nav_eur": 10000}))
    season = store.create_season(manifest())
    with pytest.raises(ValueError, match="observed-EUR"):
        append(store, season, paper_snapshot=paper_snapshot())
    assert not store.select("batches")


def usd_fx_manifest(**overrides):
    values = {"fx_observation": {"observation_id": "initial-fx", "rate_usd_to_eur": 0.9,
            "available_at": "2026-10-20T19:00:00Z", "source": "offline-fixture"},
        "paper_header": {"initial_nav_eur": 9000, "initial_cash_eur": 9000,
                      "fx_observation_id": "initial-fx"}, **overrides}
    return manifest(**values)


def test_usd_eur_header_requires_actual_observation_and_correct_conversion(store):
    with pytest.raises(ValueError, match="record"):
        store.create_season(manifest(paper_header={"initial_nav_eur": 9000, "fx_observation_id": "pointer-only"}))
    with pytest.raises(ValueError, match="does not match"):
        store.create_season(usd_fx_manifest(paper_header={"initial_nav_eur": 10000,
            "fx_observation_id": "initial-fx"}))
    assert not store.list_seasons()


def test_usd_eur_snapshot_requires_observed_ledger_fx_and_correct_native_amounts(store, journal):
    season = store.create_season(usd_fx_manifest())
    native = snapshot(hold_nav_usd=10000, benchmark_nav_usd=10000)
    paper = {**paper_snapshot(), "cash_eur": 9000, "net_nav_eur": 9000,
             "hold_nav_eur": 9000, "benchmark_nav_eur": 9000, "fx_observation_id": "daily-fx"}
    with pytest.raises(ValueError, match="not an observed"):
        append(store, season, snapshot=native, paper_snapshot=paper)
    fx = event("daily-fx", "observation", payload={"rate_usd_to_eur": 0.9, "source": "offline-fixture"})
    with pytest.raises(ValueError, match="does not match"):
        append(store, season, events=[fx], snapshot=native, paper_snapshot={**paper, "net_nav_eur": 10000})
    assert journal.list_paper_daily_runs().empty
    result = append(store, season, events=[fx], snapshot=native, paper_snapshot=paper)
    assert result["paper_daily_run_id"]
    assert journal.list_paper_daily_runs().iloc[0]["net_nav_eur"] == 9000
    assert store.list_snapshots(season["season_id"])[0]["snapshot"]["net_nav_usd"] == 10000


def test_run_identity_cannot_be_reused_for_a_different_market_date(store):
    season = store.create_season(manifest())
    append(store, season)
    with pytest.raises(ModelResultsConflict, match="different market date"):
        append(store, season, market_date="2026-10-21", snapshot=None, events=[event("other")], expected_previous_hash=None)


def test_same_event_twice_in_one_batch_is_deduplicated(store):
    season = store.create_season(manifest())
    result = append(store, season, events=[event(), event()])
    assert result["inserted_events"] == 1


def test_event_identity_is_global_to_the_season_even_on_a_revised_run(store):
    season = store.create_season(manifest())
    first = append(store, season)
    revised = append(store, season, run_id="2026-10-20-revision-2", expected_previous_hash=first["head_hash"],
                     snapshot=snapshot(net_nav_usd=10001))
    assert revised["inserted_events"] == 0
    assert len(store.list_events(season["season_id"])) == 1
    assert len(store.list_snapshots(season["season_id"])) == 2
    assert store.list_events(season["season_id"], run_id="2026-10-20-revision-2") == []
    with pytest.raises(ModelResultsConflict, match="event identity"):
        append(store, season, run_id="revision-3", snapshot=None,
               events=[event(payload={"action": "buy"})], expected_previous_hash=revised["head_hash"])


def test_batch_request_retains_hash_evidence_for_discarded_duplicate_events(store):
    season = store.create_season(manifest())
    first = append(store, season)
    append(store, season, run_id="revision-2", snapshot=snapshot(net_nav_usd=10001),
           expected_previous_hash=first["head_hash"])
    for batch in store.select("batches"):
        request = batch["request"]
        evidence = {key: value for key, value in request.items() if key not in {"batch_id", "content_hash", "content_canonical"}}
        assert content_hash(evidence) == batch["content_hash"]


def test_deterministic_identity_has_no_wall_clock_component():
    assert deterministic_event_id("A", "budget", "2026-10-20", "AAPL", "entry") == deterministic_event_id("A", "budget", "2026-10-20", "AAPL", "entry")
    assert deterministic_event_id("A", "budget") != deterministic_event_id("A", "theoretical")


def test_remote_adapter_uses_single_transactional_rpc_and_canonical_contract():
    response = Mock()
    response.json.return_value = {"season_id": "season"}
    journal = SimpleNamespace(owner="owner-a", url="https://example.supabase.co", _request=Mock(return_value=response))
    store = SupabaseModelResultsStore(journal)
    store.create_season(manifest(), season_id="season")
    call = journal._request.call_args
    assert call.args == ("POST",)
    assert call.kwargs["endpoint"].endswith("/rpc/model_results_create_season")
    request = call.kwargs["json"]["p_request"]
    assert request["paper_header"] is None
    assert request["owner"] == "owner-a"
    assert request["manifest"]["initial_nav_usd"] == 10000
    store.append_batch("season", run_id="daily", market_date="2026-10-20", events=[event()], snapshot=snapshot(), expected_previous_hash=GENESIS_HASH)
    request = journal._request.call_args.kwargs["json"]["p_request"]
    assert len(request["events"][0]["event_hash"]) == 64
    assert request["snapshot"]["payload"]["benchmark_nav_usd"] is None
    assert journal._request.call_count == 2


def test_remote_error_never_falls_back_to_nonatomic_rest():
    journal = SimpleNamespace(owner="owner-a", url="https://example.supabase.co", _request=Mock(side_effect=RuntimeError("RPC unavailable")))
    store = SupabaseModelResultsStore(journal)
    with pytest.raises(RuntimeError, match="RPC unavailable"):
        store.create_season(manifest())
    assert journal._request.call_count == 1


def test_factory_selects_existing_backend(journal):
    assert isinstance(create_model_results_store(journal), ModelResultsStore)
    remote = SimpleNamespace(owner="owner-a", url="https://example.supabase.co", _request=Mock())
    assert isinstance(create_model_results_store(remote), SupabaseModelResultsStore)
