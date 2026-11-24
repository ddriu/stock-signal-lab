"""Owner-approved, deterministic Arena universe selection; never market I/O."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import math
import re

from src.arena_data_quality import build_arena_quality_manifest
from src.arena_universe_policy import normalize_arena_universe_policy
from src.instruments import resolve_analysis_ticker

ARENA_UNIVERSE_SELECTOR_VERSION = "verified-only-universe-v1"
ARENA_UNIVERSE_AUDIT_VERSION = "arena-universe-audit-v1"
_IDENTITY = re.compile(r"[a-f0-9]{64}\Z")
_PENDING_CODES = frozenset({
    "missing_analysis", "calendar_unverified", "analysis_session_mismatch",
    "price_missing", "price_session_mismatch", "price_currency_unverified",
    "price_identity_unverified", "snapshot_identity_missing",
    "observation_time_unverified", "observation_before_close",
    "quality_unverified", "growth_incomplete",
})


def _clock(reference_time=None):
    value = datetime.now(timezone.utc) if reference_time is None else reference_time
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("La selección de Arena necesita una hora verificable.") from exc
    if value.tzinfo is None:
        raise ValueError("La selección de Arena necesita una hora con zona horaria.")
    return value.astimezone(timezone.utc)


def read_arena_universe_policy(journal, *, reference_time=None):
    """Read this journal only; failed reads never silently fall back to strict."""
    getter = getattr(journal, "get_arena_universe_policy", None)
    if getter is None:
        return None
    if not callable(getter):
        raise ValueError("La política de Arena no se puede consultar.")
    return normalize_arena_universe_policy(getter(), owner=getattr(journal, "owner", None),
        reference_time=_clock(reference_time))


def _observed_price_conflicts(rows):
    conflicts = set()
    for row in rows:
        if not isinstance(row, Mapping) or row.get("price_identity_verified") is not False:
            continue
        identity, price = row.get("snapshot_id"), row.get("price")
        if (not isinstance(identity, str) or not _IDENTITY.fullmatch(identity)
                or isinstance(price, bool) or not isinstance(price, (int, float))
                or not math.isfinite(price)):
            continue
        try:
            conflicts.add(resolve_analysis_ticker(row.get("ticker")))
        except (ValueError, TypeError):
            pass
    return conflicts


def _pending_reason(codes):
    reason = "verified_only:" + ",".join(codes)
    # Manifest-v1 bounds the reason; the audit retains every original code.
    return reason if len(reason) <= 160 else "verified_only:multiple_verification_errors"


def resolve_arena_universe(
    rows: Iterable[Mapping[str, object]], *, favorite_tickers,
    expected_session, policy=None, owner=None, reference_time=None, exclusions=None,
):
    """Return (manifest-v1, optional audit), rebuilding exclusions every time.

    Strict/default inputs produce the original manifest without a new report
    field. Verified-only requires an approved owner policy. Missing inputs may
    remain pending; observed contradictions, duplicates and global errors block.
    No score, price or favorite is modified or fabricated.
    """
    clock = _clock(reference_time)
    source = list(rows)
    favorites = None if favorite_tickers is None else tuple(favorite_tickers)
    normalized = None
    if policy is not None:
        if owner is None:
            raise ValueError("La política de Arena necesita su propietario explícito.")
        normalized = normalize_arena_universe_policy(policy, owner=owner, reference_time=clock)
    verified_only = normalized is not None and normalized["mode"] == "verified_only"
    if verified_only and exclusions:
        raise ValueError("La política verificada no admite exclusiones manuales adicionales.")
    strict = build_arena_quality_manifest(source, favorite_tickers=favorites,
        expected_session=expected_session, exclusions=exclusions, reference_time=clock.isoformat())
    if not verified_only:
        return strict, None

    codes_by_ticker = {}
    for item in strict["errors"]:
        if item.get("ticker"):
            codes_by_ticker.setdefault(item["ticker"], set()).add(item["code"])
    conflicts = _observed_price_conflicts(source)
    pending = []
    for ticker, codes in sorted(codes_by_ticker.items()):
        if ticker in conflicts or not codes.issubset(_PENDING_CODES):
            continue
        values = sorted(codes)
        pending.append({"ticker": ticker, "codes": values, "reason": _pending_reason(values)})
    derived = {item["ticker"]: item["reason"] for item in pending}
    effective = build_arena_quality_manifest(source, favorite_tickers=favorites,
        expected_session=expected_session, exclusions=derived, reference_time=clock.isoformat())
    global_errors = [dict(item) for item in strict["errors"] if item.get("ticker") not in derived]
    for item in effective["errors"]:
        if item not in global_errors:
            global_errors.append(dict(item))
    global_errors.sort(key=lambda item: (item.get("ticker") or "", item["code"]))
    audit = {
        "version": ARENA_UNIVERSE_AUDIT_VERSION, "mode": "verified_only",
        "policy": dict(normalized), "target_session": strict["expected_session"],
        "required_count": strict["required_count"],
        "initial_eligible_count": strict["eligible_count"], "initial_valid_count": strict["valid_count"],
        "eligible_count": effective["eligible_count"], "valid_count": effective["valid_count"],
        "pending_count": len(pending), "pending": pending,
        "global_errors": global_errors,
        "strict_errors": [dict(item) for item in strict["errors"]],
    }
    return effective, audit
