"""Synthetic, deterministic fixtures shared by Python and isolated SQL QA.

No provider, backend, SMTP or Arena operation is used by this module.
"""
from __future__ import annotations

from copy import deepcopy

from src.arena_universe import resolve_arena_universe
from src.arena_universe_policy import build_arena_universe_policy
from src.daily_digest import seal_daily_score_report

DAY = "2026-11-23"
CLOCK = "2026-11-24T16:13:00Z"


def build_fixtures():
    policy = build_arena_universe_policy("ddriu", "verified_only",
        approved_at="2026-11-24T15:00:00Z", reference_time=CLOCK)
    strict = build_arena_universe_policy("ddriu", reference_time=CLOCK)
    changed = build_arena_universe_policy("ddriu", "verified_only",
        approved_at="2026-11-24T15:30:00Z", reference_time=CLOCK)
    base = {"ticker": "GOOD", "session": DAY, "calendar": "XNYS",
        "score": 80, "growth_score": 70, "momentum_score": 80, "context_score": 90,
        "status": "entry_strong", "primary_data_status": "complete",
        "quality": {"block_entry": False, "status": "ok"},
        "snapshot_id": "a" * 64, "recorded_at": DAY + "T22:00:00Z", "price": 100.0,
        "price_session": DAY, "price_currency": "USD", "price_source": "analysis_snapshot",
        "price_identity_verified": True}

    def report(rows, *, selected_policy=policy):
        manifest, audit = resolve_arena_universe(rows,
            favorite_tickers=["GOOD", "PENDING"], expected_session=DAY,
            policy=selected_policy, owner="ddriu", reference_time=CLOCK)
        result = {"as_of": DAY, "rows": deepcopy(rows), "arena_quality": manifest}
        if audit is not None:
            result["arena_universe"] = audit
        return seal_daily_score_report(result)

    pending = {**base, "ticker": "PENDING", "snapshot_id": "b" * 64}
    return {"policy": policy, "strict_policy": strict, "changed_policy": changed,
        "verified": report([base]),
        "growth_pending": report([base, {**pending, "growth_score": None}]),
        "quality_pending": report([base, {**pending, "price": None,
            "quality": {"block_entry": True, "status": "blocked"}}]),
        "all_pending": report([]),
        "conflict": report([base, {**pending, "price_identity_verified": False}]),
        "complete": report([base, pending]),
        "strict": report([base], selected_policy=strict)}
