"""Deterministic owner consent and dynamically re-admitted Arena inputs."""
from copy import deepcopy
import importlib.util
import subprocess
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from src.analysis_policy import encode_policy_note
from src.arena_data_quality import arena_report_authorized, build_arena_quality_manifest
from src.arena_universe import read_arena_universe_policy, resolve_arena_universe
from src.arena_universe_policy import build_arena_universe_policy
from src.daily_digest import build_daily_score_report, seal_daily_score_report

DAY, CLOCK = "2026-11-23", "2026-11-24T16:13:00Z"
OWNER = "synthetic-owner"


def consent(owner=OWNER):
    return build_arena_universe_policy(owner, "verified_only", approved_at="2026-11-24T15:00:00Z", reference_time=CLOCK)


def row(ticker="GOOD", **changes):
    value = {"ticker": ticker, "session": DAY, "calendar": "XNYS",
        "score": 80, "growth_score": 70, "momentum_score": 80, "context_score": 90,
        "status": "entry_strong", "primary_data_status": "complete",
        "quality": {"block_entry": False, "status": "ok"},
        "snapshot_id": "a" * 64, "recorded_at": DAY + "T22:00:00Z", "price": 100.,
        "price_session": DAY, "price_currency": "USD", "price_source": "analysis_snapshot",
        "price_identity_verified": True}
    value.update(changes)
    return value


def select(rows, favorites=("GOOD", "PENDING"), **kwargs):
    args = {"favorite_tickers": favorites, "expected_session": DAY, "policy": consent(),
        "owner": OWNER, "reference_time": CLOCK}
    args.update(kwargs)
    return resolve_arena_universe(rows, **args)


def observation(ticker="GOOD", score=80):
    policy = {"policy_version": "dual_strategy_v1", "snapshot_method": "dual", "ticker": ticker,
        "as_of": DAY, "source": "Synthetic provider", "price": 100., "currency": "USD", "config": {},
        "primary": {"id": "growth_momentum", "strategy_id": "growth_momentum", "score": score,
            "growth_score": 70, "momentum_score": 80, "context_score": 90,
            "status": "Entrada fuerte", "data_status": "complete"}, "comparative": {},
        "position": {"invalidated": False}, "data_quality": {"status": "ok", "block_entry": False, "reasons": []}}
    return {"id": 1, "ticker": ticker, "analyzed_at": DAY, "created_at": DAY + "T22:00:00Z",
        "price": 100., "note": encode_policy_note("Synthetic", policy)}


def report(**kwargs):
    args = {"tickers": ["GOOD", "PENDING"], "favorite_tickers": ["GOOD", "PENDING"],
        "as_of": DAY, "reference_time": CLOCK,
        "arena_universe_policy": consent(), "arena_policy_owner": OWNER}
    args.update(kwargs)
    return build_daily_score_report(pd.DataFrame([observation()]), **args)


def test_default_strict_manifest_is_byte_identical_without_audit():
    rows = [row()]
    expected = build_arena_quality_manifest(rows, favorite_tickers=["GOOD", "PENDING"],
        expected_session=DAY, reference_time=CLOCK)
    actual, audit = select(rows, policy=None, owner=None)
    assert actual == expected and audit is None
    assert actual["authorized"] is False


def test_default_strict_report_id_matches_pre_policy_published_builder():
    # Load only the old pure builder in a private namespace, no sys.modules reset.
    spec = importlib.util.find_spec("src.daily_digest")
    code = subprocess.check_output(["git", "show", "cd69d01:src/daily_digest.py"], text=True)
    old = importlib.util.module_from_spec(spec)
    exec(compile(code, "<published pure digest>", "exec"), old.__dict__)
    args = {"tickers": ["GOOD", "PENDING"], "favorite_tickers": ["GOOD", "PENDING"],
        "as_of": DAY, "reference_time": CLOCK}
    source = pd.DataFrame([observation()])
    expected = old.build_daily_score_report(source, **args)
    current = build_daily_score_report(source, **args)
    strict_policy = build_arena_universe_policy(OWNER, reference_time=CLOCK)
    explicit = build_daily_score_report(source, **args,
        arena_universe_policy=strict_policy, arena_policy_owner=OWNER)
    assert expected == current == explicit
    assert "arena_universe" not in current


def test_verified_policy_keeps_favorites_and_records_dynamic_pending_reasons():
    rows = [row()]
    original = deepcopy(rows)
    manifest, audit = select(rows)
    assert manifest["authorized"] is True
    assert manifest["favorite_tickers"] == ["GOOD", "PENDING"]
    assert manifest["eligible_tickers"] == ["GOOD"]
    assert audit["required_count"] == 2
    assert audit["initial_eligible_count"] == 2 and audit["initial_valid_count"] == 1
    assert audit["eligible_count"] == audit["valid_count"] == audit["pending_count"] == 1
    assert audit["pending"] == [{"ticker": "PENDING", "codes": ["missing_analysis"],
        "reason": "verified_only:missing_analysis"}]
    assert audit["strict_errors"] == [{"ticker": "PENDING", "code": "missing_analysis"}]
    assert audit["global_errors"] == []
    assert rows == original


def test_recovered_instrument_is_automatically_readmitted_without_frozen_counts():
    first, first_audit = select([row()])
    later, later_audit = select([row(), row("PENDING")])
    assert first["eligible_tickers"] == ["GOOD"] and first_audit["pending_count"] == 1
    assert later["eligible_tickers"] == ["GOOD", "PENDING"]
    assert later["explicit_exclusions"] == [] and later_audit["pending"] == []
    assert later_audit["pending_count"] == 0 and later_audit["valid_count"] == 2


def test_scores_or_performance_do_not_choose_the_verified_universe():
    manifest, audit = select([row(score=0), row("PENDING", score=100)])
    assert manifest["eligible_tickers"] == ["GOOD", "PENDING"] and audit["pending_count"] == 0
    with pytest.raises(ValueError, match="exclusiones manuales"):
        select([row(), row("PENDING")], exclusions={"GOOD": "avoid low performance"})


@pytest.mark.parametrize("rows,kwargs,code", [
    ([row(), row()], {}, "duplicate_analysis"),
    ([row()], {"favorite_tickers": None}, "favorite_universe_unknown"),
    ([row()], {"expected_session": "2026-11-24"}, "expected_session_not_closed"),
    ([row(), row("PENDING", recorded_at="2026-11-25T12:00:00Z")], {}, "observation_after_reference"),
    ([row(), row("PENDING", price_identity_verified=False)], {}, "price_identity_unverified"),
])
def test_global_or_observed_integrity_failures_are_never_hidden(rows, kwargs, code):
    manifest, audit = select(rows, **kwargs)
    assert manifest["authorized"] is False
    assert code in {item["code"] for item in manifest["errors"]}
    assert code in {item["code"] for item in audit["global_errors"]}


def test_all_pending_cannot_authorize_an_empty_universe():
    manifest, audit = select([])
    assert manifest["authorized"] is False and manifest["eligible_tickers"] == []
    assert {item["code"] for item in manifest["errors"]} == {"no_eligible_instruments"}
    assert audit["pending_count"] == 2
    assert audit["global_errors"] == [{"ticker": None, "code": "no_eligible_instruments"}]


@pytest.mark.parametrize("policy,owner", [
    ({"mode": "verified_only"}, OWNER), (consent(), "different-owner"), (consent(), None),
    ({**consent(), "policy_id": "f" * 64}, OWNER),
])
def test_approval_is_a_valid_owner_hash_not_a_mode_flag(policy, owner):
    with pytest.raises(ValueError):
        select([row()], policy=policy, owner=owner)


def test_report_verifier_rebuilds_manifest_and_audit_not_self_asserted_exclusions():
    valid = report()
    assert arena_report_authorized(valid, owner=OWNER) is True
    assert arena_report_authorized(valid, owner="different-owner") is False
    for alter in (lambda value: value["arena_universe"].update(pending=[]),
                  lambda value: value["arena_universe"].update(valid_count=999),
                  lambda value: value["arena_universe"].update(pending_count=True),
                  lambda value: value["arena_universe"]["policy"].update(policy_id="f" * 64),
                  lambda value: value["arena_quality"]["explicit_exclusions"].append({"ticker": "GOOD", "reason": "performance"})):
        tampered = deepcopy(valid)
        alter(tampered)
        assert arena_report_authorized(seal_daily_score_report(tampered), owner=OWNER) is False


def test_current_policy_context_rejects_removing_audit_and_resealing_as_legacy():
    value = report()
    del value["arena_universe"]
    assert arena_report_authorized(seal_daily_score_report(value), owner=OWNER,
        universe_policy=consent()) is False


def test_normal_legacy_owner_exclusions_keep_the_existing_contract():
    value = report(arena_universe_policy=None, arena_policy_owner=None,
        arena_exclusions={"PENDING": "owner decision recorded before the new policy"})
    assert "arena_universe" not in value
    assert arena_report_authorized(value) is True


def test_two_owners_never_share_a_verified_only_flag_or_policy_identity():
    owner_a, owner_b = "synthetic-a", "synthetic-b"
    first = SimpleNamespace(owner=owner_a, get_arena_universe_policy=lambda: consent(owner_a))
    second = SimpleNamespace(owner=owner_b, get_arena_universe_policy=lambda: None)
    a_policy = read_arena_universe_policy(first, reference_time=CLOCK)
    b_policy = read_arena_universe_policy(second, reference_time=CLOCK)
    a_manifest, a_audit = select([row()], owner=owner_a, policy=a_policy)
    b_manifest, b_audit = select([row()], owner=owner_b, policy=b_policy)
    assert a_manifest["authorized"] is True and a_audit["policy"]["owner"] == owner_a
    assert b_policy["mode"] == "strict" and b_manifest["authorized"] is False and b_audit is None


def test_future_approval_is_not_accepted_even_with_a_self_consistent_policy_hash():
    future = build_arena_universe_policy(OWNER, "verified_only",
        approved_at="2026-11-25T12:00:00Z", reference_time="2026-11-25T13:00:00Z")
    with pytest.raises(ValueError, match="futuro"):
        select([row()], policy=future)


def test_verifier_with_current_database_policy_rejects_revoked_or_replaced_consent():
    value = report()
    assert arena_report_authorized(value, owner=OWNER, universe_policy=consent()) is True
    revoked = build_arena_universe_policy(OWNER, reference_time=CLOCK)
    replaced = build_arena_universe_policy(OWNER, "verified_only",
        approved_at="2026-11-24T15:30:00Z", reference_time=CLOCK)
    assert arena_report_authorized(value, owner=OWNER, universe_policy=revoked) is False
    assert arena_report_authorized(value, owner=OWNER, universe_policy=replaced) is False
    legacy = report(arena_universe_policy=None, arena_policy_owner=None,
        arena_exclusions={"PENDING": "legacy owner choice"})
    assert arena_report_authorized(legacy) is True
    assert arena_report_authorized(legacy, owner=OWNER, universe_policy=consent()) is False


def test_policy_reader_is_owner_local_and_never_hides_storage_failures():
    assert read_arena_universe_policy(object(), reference_time=CLOCK) is None
    policy = consent()
    journal = SimpleNamespace(owner=OWNER, get_arena_universe_policy=lambda: policy)
    assert read_arena_universe_policy(journal, reference_time=CLOCK) == policy
    journal.owner = "different-owner"
    with pytest.raises(ValueError):
        read_arena_universe_policy(journal, reference_time=CLOCK)
    def fail():
        raise RuntimeError("policy storage unavailable")
    journal.get_arena_universe_policy = fail
    with pytest.raises(RuntimeError, match="storage unavailable"):
        read_arena_universe_policy(journal, reference_time=CLOCK)
