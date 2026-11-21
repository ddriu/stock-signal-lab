"""Strict Arena data authorization with synthetic observations only."""
from copy import deepcopy

import pandas as pd
import pytest

from src.analysis_policy import encode_policy_note
from src.daily_digest import build_daily_score_report, seal_daily_score_report

DAY = "2026-11-20"
CLOCK = "2026-11-20T23:00:00Z"


def row(ticker="SYNTH", **changes):
    value = {"ticker": ticker, "session": DAY, "calendar": "XNYS",
        "score": 80, "growth_score": 70, "momentum_score": 80, "context_score": 90,
        "status": "entry_strong", "primary_data_status": "complete",
        "quality": {"block_entry": False, "status": "ok"},
        "snapshot_id": "a" * 64, "recorded_at": DAY + "T22:00:00Z",
        "price": 100.0, "price_session": DAY, "price_currency": "USD",
        "price_source": "analysis_snapshot", "price_identity_verified": True}
    value.update(changes)
    return value


def manifest(rows=None, **changes):
    from src.arena_data_quality import build_arena_quality_manifest
    arguments = {"favorite_tickers": ["SYNTH"], "expected_session": DAY,
        "reference_time": CLOCK}
    arguments.update(changes)
    return build_arena_quality_manifest([row()] if rows is None else rows, **arguments)


def observation(ticker="SYNTH", **changes):
    policy = {"policy_version": "dual_strategy_v1", "snapshot_method": "dual",
        "ticker": ticker, "as_of": DAY, "source": "Synthetic provider",
        "price": 100.0, "currency": "USD", "config": {},
        "primary": {"id": "growth_momentum", "strategy_id": "growth_momentum",
            "score": 80, "growth_score": 70, "momentum_score": 80,
            "context_score": 90, "status": "Entrada fuerte", "data_status": "complete"},
        "comparative": {}, "position": {"invalidated": False},
        "data_quality": {"status": "ok", "block_entry": False, "reasons": []}}
    raw = {"id": 1, "ticker": ticker, "analyzed_at": DAY,
        "created_at": DAY + "T22:00:00Z", "price": 100.0}
    policy.update(changes.pop("policy", {}))
    raw.update(changes)
    raw["note"] = encode_policy_note("Synthetic", policy)
    return raw


def test_daily_report_seals_explicit_favorite_manifest_and_price_evidence():
    report = build_daily_score_report(pd.DataFrame([observation()]),
        tickers=["SYNTH"], favorite_tickers=["SYNTH"], as_of=DAY, reference_time=CLOCK)
    assert report["arena_quality"]["authorized"] is True
    assert report["arena_quality"]["price_ready"] is True
    assert report["rows"][0]["price"] == 100
    assert report["rows"][0]["price_currency"] == "USD"
    assert report["rows"][0]["price_identity_verified"] is True
    assert seal_daily_score_report(report)["report_id"] == report["report_id"]


def test_partial_universe_is_not_authorized_by_one_available_signal():
    value = manifest(favorite_tickers=["SYNTH", "MISSING"])
    assert value["authorized"] is False
    assert value["required_count"] == 2 and value["valid_count"] == 1
    assert {item["code"] for item in value["errors"]} >= {"missing_analysis"}


def test_full_known_inputs_are_authorized_without_valuation_metrics():
    value = manifest()
    assert value["authorized"] is True
    assert value["eligible_count"] == value["valid_count"] == 1
    assert value["verification_scope"] == "persisted_snapshot_checks"
    assert "valuation" not in str(value["criteria"])


def test_missing_favorite_universe_does_not_infer_it_from_held_rows():
    value = manifest(favorite_tickers=None)
    assert value["authorized"] is False
    assert {item["code"] for item in value["errors"]} >= {"favorite_universe_unknown"}


def test_held_only_instrument_is_not_an_eligible_favorite():
    value = manifest([row(), row("HELD")])
    assert value["eligible_tickers"] == ["SYNTH"]
    assert value["required_count"] == 1


def test_foreign_benchmark_and_explicit_owner_exclusions_are_annotated():
    value = manifest(favorite_tickers=["SYNTH", "EU.MC", "SPY", "MISSING"],
        exclusions={"MISSING": "owner_excluded"})
    assert value["authorized"] is True
    assert {item["ticker"] for item in value["excluded"]} == {"EU.MC", "SPY", "MISSING"}


def test_known_quality_veto_is_explicitly_excluded_not_a_global_growth_veto():
    blocked = row("VETO", status="review_data", quality={"status": "blocked", "block_entry": True})
    value = manifest([row(), blocked], favorite_tickers=["SYNTH", "VETO"])
    assert value["authorized"] is True
    assert value["required_count"] == 2
    assert value["eligible_tickers"] == ["SYNTH"]
    assert {item["code"] for item in value["excluded"]} == {"quality_policy_blocked"}


def test_unknown_quality_is_not_silently_excluded():
    value = manifest([row(quality={"status": "unknown", "block_entry": True})])
    assert value["authorized"] is False
    assert value["excluded"] == []


@pytest.mark.parametrize("changes,code", [
    ({"price": None}, "price_missing"), ({"price": float("nan")}, "price_missing"),
    ({"price": -1}, "price_missing"), ({"price": True}, "price_missing"),
    ({"price_currency": "EUR"}, "price_currency_unverified"),
    ({"price_currency": {}}, "price_currency_unverified"),
    ({"price": 0.00001}, "price_missing"), ({"price": 1_000_001}, "price_missing"),
    ({"price_identity_verified": False}, "price_identity_unverified"),
    ({"price_session": "2026-11-19"}, "price_session_mismatch"),
    ({"session": "2026-11-19"}, "analysis_session_mismatch"),
    ({"calendar": "unknown"}, "calendar_unverified"),
    ({"recorded_at": "2026-11-20T20:00:00Z"}, "observation_before_close"),
    ({"recorded_at": "2026-11-21T00:00:00Z"}, "observation_after_reference"),
    ({"recorded_at": "2026-11-20T22:00:00"}, "observation_time_unverified"),
    ({"snapshot_id": None}, "snapshot_identity_missing"),
    ({"growth_score": None}, "growth_incomplete"),
    ({"momentum_score": None}, "growth_incomplete"),
    ({"primary_data_status": "partial"}, "growth_incomplete"),
])
def test_critical_unknown_inputs_fail_closed(changes, code):
    value = manifest([row(**changes)])
    assert value["authorized"] is False
    assert code in {item["code"] for item in value["errors"]}


def test_even_quality_veto_does_not_hide_a_missing_price():
    value = manifest([row(price=None, quality={"status": "blocked", "block_entry": True})])
    assert value["authorized"] is False and value["price_ready"] is False


def test_duplicate_rows_and_empty_universe_cannot_authorize():
    assert manifest([row(), row()])["authorized"] is False
    assert manifest([], favorite_tickers=[])["authorized"] is False


def test_expected_session_must_be_completed_exchange_session():
    assert manifest(expected_session="2026-11-21")["authorized"] is False
    assert manifest(reference_time="2026-11-20T20:59:59Z")["authorized"] is False


def test_manifest_order_and_later_clock_do_not_change_identity():
    first = manifest([row("TWO"), row()], favorite_tickers=["TWO", "SYNTH"])
    second = manifest([row(), row("TWO")], favorite_tickers=["SYNTH", "TWO"],
        reference_time="2026-11-21T01:00:00Z")
    assert first == second


def test_report_verifier_rejects_legacy_or_tampered_evidence():
    from src.arena_data_quality import arena_report_authorized
    legacy = seal_daily_score_report({"as_of": DAY, "rows": [row()]})
    assert arena_report_authorized(legacy) is False
    valid = seal_daily_score_report({"as_of": DAY, "rows": [row()], "arena_quality": manifest()})
    assert arena_report_authorized(valid) is True
    tampered = deepcopy(valid)
    tampered["rows"][0]["price"] = 200
    assert arena_report_authorized(tampered) is False
    tampered = seal_daily_score_report(tampered)
    assert arena_report_authorized(tampered) is False


def test_raw_and_sealed_policy_prices_must_agree_in_report():
    report = build_daily_score_report(pd.DataFrame([observation(price=200)]),
        tickers=["SYNTH"], favorite_tickers=["SYNTH"], as_of=DAY, reference_time=CLOCK)
    assert report["arena_quality"]["authorized"] is False
    assert report["rows"][0]["price_identity_verified"] is False
