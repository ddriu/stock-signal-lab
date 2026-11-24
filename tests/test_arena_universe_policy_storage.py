"""Explicit owner consent persists separately from watchlists, orders and mail."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.arena_universe_policy import (
    ARENA_UNIVERSE_POLICY_VERSION, build_arena_universe_policy,
    normalize_arena_universe_policy,
)
from src.journal import TradingJournal
from src.session_journal import session_journal
from src.supabase_journal import JournalStorageError, SupabaseTradingJournal

APPROVAL = "2026-11-24T10:00:00Z"
CLOCK = "2026-11-24T11:00:00Z"


def policy(owner="alice", mode="verified_only"):
    return build_arena_universe_policy(owner, mode, approved_at=APPROVAL, reference_time=CLOCK)


def test_default_is_strict_stable_json_safe_and_never_implies_consent():
    first = normalize_arena_universe_policy(None, owner="alice", reference_time=CLOCK)
    assert first == normalize_arena_universe_policy(None, owner="alice", reference_time="2026-11-25T11:00:00Z")
    assert first["owner"] == "alice" and first["mode"] == "strict"
    assert first["approved_at"] is None and len(first["policy_id"]) == 64
    assert first["version"] == ARENA_UNIVERSE_POLICY_VERSION == "arena-universe-policy-v1"
    assert first["policy_id"] != normalize_arena_universe_policy(None, owner="bob")["policy_id"]


def test_policy_hash_owner_approval_and_version_are_bound_to_the_exact_content():
    value = policy()
    assert normalize_arena_universe_policy(value, owner="alice", reference_time=CLOCK) == value
    assert value["approved_at"] == "2026-11-24T10:00:00.000000+00:00"
    for field, replacement in (("owner", "bob"), ("mode", "strict"),
            ("approved_at", None), ("version", "unknown-v2"), ("policy_id", "f" * 64)):
        with pytest.raises(ValueError):
            normalize_arena_universe_policy({**value, field: replacement}, owner="alice", reference_time=CLOCK)


@pytest.mark.parametrize("stamp", ["2026-11-24T12:00:00Z", "2026-11-24T10:00:00", "invalid", True])
def test_future_unzoned_or_invalid_consent_is_never_accepted(stamp):
    with pytest.raises(ValueError):
        build_arena_universe_policy("alice", "verified_only", approved_at=stamp, reference_time=CLOCK)
    with pytest.raises(ValueError):
        build_arena_universe_policy("alice", "verified_only", reference_time=CLOCK)


def test_sqlite_read_does_not_create_policy_and_set_is_scoped_idempotent(tmp_path):
    path = tmp_path / "policy.sqlite"
    alice, bob = TradingJournal(path, owner="alice"), TradingJournal(path, owner="bob")
    before = alice.get_arena_universe_policy()
    assert before["mode"] == "strict" and before["approved_at"] is None
    with alice._connect() as connection:
        assert connection.execute("select count(*) from arena_universe_policies").fetchone()[0] == 0
    with pytest.raises(ValueError):
        alice.set_arena_universe_policy("verified_only", approved_at=APPROVAL)
    approved = alice.set_arena_universe_policy("verified_only", owner_consent=True, approved_at=APPROVAL)
    assert approved == policy()
    assert alice.set_arena_universe_policy("verified_only", owner_consent=True) == approved
    assert TradingJournal(path, owner="alice").get_arena_universe_policy() == approved
    assert bob.get_arena_universe_policy()["mode"] == "strict"
    assert before != approved
    with alice._connect() as connection:
        assert connection.execute("select count(*) from arena_universe_policies").fetchone()[0] == 1
        for table in ("operations", "favorites", "daily_score_reports", "daily_digest_deliveries"):
            assert connection.execute(f"select count(*) from {table}").fetchone()[0] == 0


@pytest.mark.parametrize("owner", ["", "anon", "anonymous", "public", "grupo_compartido", "group", " alice", "alice&owner=eq.bob"])
def test_reserved_or_implicit_owner_cannot_read_or_enable_arena_policy(tmp_path, owner):
    journal = TradingJournal(tmp_path / "implicit.sqlite", owner=owner)
    with pytest.raises(ValueError):
        journal.get_arena_universe_policy()
    with pytest.raises(ValueError):
        journal.set_arena_universe_policy("verified_only", owner_consent=True, approved_at=APPROVAL)


def remote(owner="alice"):
    return SupabaseTradingJournal("https://synthetic.invalid", "synthetic-backend-key", owner)


def test_supabase_exact_owner_filter_and_confirmed_write_are_required(monkeypatch):
    journal = remote()
    calls = []
    stored = []

    def request(method, **kwargs):
        calls.append((method, deepcopy(kwargs)))
        if method == "POST":
            stored[:] = [deepcopy(kwargs["json"])]
        return SimpleNamespace(json=lambda: deepcopy(stored))

    monkeypatch.setattr(journal, "_request", request)
    assert journal.get_arena_universe_policy()["mode"] == "strict"
    saved = journal.set_arena_universe_policy("verified_only", owner_consent=True, approved_at=APPROVAL)
    assert saved == policy()
    assert journal.get_arena_universe_policy() == saved
    assert journal.set_arena_universe_policy("verified_only", owner_consent=True) == saved
    assert sum(method == "POST" for method, _ in calls) == 1
    for method, kwargs in calls:
        assert kwargs["endpoint"].endswith("/rest/v1/arena_universe_policies")
        if method == "GET":
            assert kwargs["params"]["owner"] == "eq.alice" and kwargs["params"]["limit"] == "2"
        else:
            assert kwargs["json"]["owner"] == "alice"
            assert kwargs["params"]["on_conflict"] == "owner"


@pytest.mark.parametrize("rows", [[policy("bob")], [policy(), policy()],
    [{**policy(), "policy_id": "0" * 64}], {"not": "rows"}])
def test_supabase_bad_owner_tampering_or_ambiguous_response_never_becomes_strict(monkeypatch, rows):
    journal = remote()
    monkeypatch.setattr(journal, "_request", lambda *_args, **_kwargs: SimpleNamespace(json=lambda: rows))
    with pytest.raises(JournalStorageError):
        journal.get_arena_universe_policy()


def test_missing_remote_migration_is_not_a_silent_strict_default(monkeypatch):
    journal = remote()

    def absent(*_args, **_kwargs):
        raise JournalStorageError("Synthetic missing table", status_code=404)

    monkeypatch.setattr(journal, "_request", absent)
    with pytest.raises(JournalStorageError):
        journal.get_arena_universe_policy()


def test_supabase_ambiguous_committed_write_is_not_claimed_as_approved(monkeypatch):
    journal = remote()
    gets = iter([[], [policy("bob")]])
    monkeypatch.setattr(journal, "_request", lambda method, **_kwargs: SimpleNamespace(
        json=lambda: next(gets) if method == "GET" else []))
    with pytest.raises(JournalStorageError):
        journal.set_arena_universe_policy("verified_only", owner_consent=True, approved_at=APPROVAL)


def test_policy_read_is_uncached_and_set_invalidates_session_views():
    class Backend:
        owner = "alice"

        def __init__(self):
            self.calls = Counter()
            self.value = normalize_arena_universe_policy(None, owner=self.owner)

        def get_arena_universe_policy(self):
            self.calls["get"] += 1
            return self.value

        def set_arena_universe_policy(self, *_args, **_kwargs):
            self.calls["set"] += 1
            self.value = policy()
            return self.value

    backend, state = Backend(), {}
    proxy = session_journal(backend, state)
    assert proxy.get_arena_universe_policy()["mode"] == "strict"
    backend.value = policy()
    assert proxy.get_arena_universe_policy()["mode"] == "verified_only"
    assert backend.calls["get"] == 2 and "_view_data_revision" not in state
    proxy.set_arena_universe_policy("verified_only", owner_consent=True)
    assert state["_view_data_revision"] == 1
    value = proxy.get_arena_universe_policy()
    value["mode"] = "MUTATED"
    assert backend.value["mode"] == "verified_only"
    backend.owner = "bob"
    with pytest.raises(RuntimeError):
        proxy.get_arena_universe_policy()
