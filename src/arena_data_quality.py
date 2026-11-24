"""Fail-closed evidence contract for the Arena's US-favorite universe.

This checks persisted inputs, not the truth of an independent market feed.
It never downloads quotes, changes scores, creates orders or writes data.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from functools import lru_cache
import hashlib
import json
import math
import re

from src.instruments import is_isin_identifier, resolve_analysis_ticker

ARENA_QUALITY_VERSION = "arena-data-quality-v1"
ARENA_SCOPE_VERSION = "isin-market-scope-v1"
_US_SYMBOL = re.compile(r"[A-Z][A-Z0-9-]{0,11}\Z")
_IDENTITY = re.compile(r"[0-9a-f]{64}\Z")


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False)


def _symbol(value):
    try:
        return resolve_analysis_ticker(str(value or "").strip())
    except (ValueError, TypeError):
        return ""


def _number(value, *, score=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or (not 0 <= value <= 100 if score else not 0.0001 <= value <= 1_000_000):
        return None
    # Canonical JSON must survive Python -> JSONB -> JavaScript round trips.
    # In the bounded price range, non-integral values use decimal JSON and
    # integral evidence is serialized as an integer in both runtimes.
    return int(value) if value.is_integer() else value


def _stamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _day(value):
    try:
        text = str(value or "")
        return text if date.fromisoformat(text).isoformat() == text else None
    except (ValueError, TypeError):
        return None


@lru_cache(maxsize=256)
def _session_close(session):
    try:
        import exchange_calendars
        return exchange_calendars.get_calendar("XNYS").session_close(session).to_pydatetime()
    except (ValueError, KeyError, TypeError, ImportError):
        return None


def build_arena_quality_manifest(
    rows: Iterable[Mapping[str, object]], *,
    favorite_tickers: Iterable[str] | None,
    expected_session: str | None,
    exclusions: Mapping[str, str] | None = None,
    reference_time: object | None = None,
) -> dict[str, object]:
    """Authorize only complete required inputs, with auditable exclusions.

    A dated, known policy veto excludes that instrument from new purchases;
    it is not a missing-input exception. Unknown prices or observation identity
    still block the report. Missing valuation is deliberately not a criterion.
    """
    expected = _day(expected_session)
    clock = (datetime.now(timezone.utc) if reference_time is None
             else _stamp(str(reference_time)))
    close = _session_close(expected) if expected else None
    errors, excluded, evidence = [], [], []
    criteria = {
        "favorite_universe_known": favorite_tickers is not None,
        "expected_session_closed": bool(close and clock and close <= clock),
        "unique_rows": True, "all_current_prices": True,
        "all_current_growth": True, "all_calendar_verified": True,
        "all_eligible_quality_unblocked": True, "all_snapshot_evidence": True,
        "has_eligible_instruments": False,
    }

    def error(ticker, code):
        item = {"ticker": ticker or None, "code": code}
        if item not in errors:
            errors.append(item)

    if not criteria["favorite_universe_known"]:
        error(None, "favorite_universe_unknown")
    if not criteria["expected_session_closed"]:
        error(None, "expected_session_not_closed")
    favorites = sorted({_symbol(value) for value in favorite_tickers or ()} - {""})
    explicit = {}
    for ticker, reason in (exclusions or {}).items():
        symbol = _symbol(ticker)
        if symbol in favorites and isinstance(reason, str) and reason.strip() and len(reason) <= 160:
            explicit[symbol] = " ".join(reason.split())
    by_ticker = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        ticker = _symbol(row.get("ticker"))
        if ticker not in favorites:
            continue
        if ticker in by_ticker:
            criteria["unique_rows"] = False
            error(ticker, "duplicate_analysis")
        by_ticker[ticker] = row
    required, eligible, valid = [], [], []
    for ticker in favorites:
        if ticker in explicit:
            excluded.append({"ticker": ticker, "code": "owner_excluded", "reason": explicit[ticker]})
            continue
        if ticker == "SPY" or is_isin_identifier(ticker) or not _US_SYMBOL.fullmatch(ticker):
            code = "benchmark_only" if ticker == "SPY" else "outside_us_market_scope"
            excluded.append({"ticker": ticker, "code": code, "reason": code})
            continue
        required.append(ticker)
        row = by_ticker.get(ticker)
        if row is None:
            eligible.append(ticker)
            error(ticker, "missing_analysis")
            for key in ("all_current_prices", "all_current_growth", "all_calendar_verified",
                        "all_eligible_quality_unblocked", "all_snapshot_evidence"):
                criteria[key] = False
            continue
        before = len(errors)
        if row.get("calendar") != "XNYS":
            criteria["all_calendar_verified"] = False
            error(ticker, "calendar_unverified")
        if row.get("session") != expected:
            criteria["all_calendar_verified"] = False
            error(ticker, "analysis_session_mismatch")
        price = _number(row.get("price"))
        for condition, code in (
            (price is not None, "price_missing"),
            (row.get("price_session") == expected, "price_session_mismatch"),
            (row.get("price_currency") == "USD", "price_currency_unverified"),
            (row.get("price_source") == "analysis_snapshot"
                and row.get("price_identity_verified") is True, "price_identity_unverified"),
        ):
            if not condition:
                criteria["all_current_prices"] = False
                error(ticker, code)
        identity = row.get("snapshot_id")
        if not isinstance(identity, str) or not _IDENTITY.fullmatch(identity):
            criteria["all_snapshot_evidence"] = False
            error(ticker, "snapshot_identity_missing")
            identity = None
        recorded = _stamp(row.get("recorded_at"))
        if recorded is None:
            criteria["all_snapshot_evidence"] = False
            error(ticker, "observation_time_unverified")
        elif close is not None and recorded < close:
            criteria["all_snapshot_evidence"] = False
            error(ticker, "observation_before_close")
        elif clock is None or recorded > clock:
            criteria["all_snapshot_evidence"] = False
            error(ticker, "observation_after_reference")
        quality = row.get("quality")
        quality = quality if isinstance(quality, Mapping) else {}
        known_veto = quality.get("status") == "blocked" and quality.get("block_entry") is True
        evidence.append({"ticker": ticker, "session": row.get("session") if _day(row.get("session")) else None,
            "snapshot_id": identity, "recorded_at": recorded.isoformat() if recorded else None,
            "price": price, "price_source": "analysis_snapshot" if row.get("price_source") == "analysis_snapshot" else None,
            "price_currency": row.get("price_currency") if isinstance(row.get("price_currency"), str)
                and row.get("price_currency") in {"USD", "EUR", "GBP"} else None})
        if known_veto:
            excluded.append({"ticker": ticker, "code": "quality_policy_blocked", "reason": "quality_policy_blocked"})
            continue
        eligible.append(ticker)
        if quality.get("block_entry") is not False or row.get("status") == "review_data":
            criteria["all_eligible_quality_unblocked"] = False
            error(ticker, "quality_unverified")
        if (row.get("primary_data_status") != "complete"
                or any(_number(row.get(key), score=True) is None
                       for key in ("score", "growth_score", "momentum_score", "context_score"))):
            criteria["all_current_growth"] = False
            error(ticker, "growth_incomplete")
        if len(errors) == before:
            valid.append(ticker)
    criteria["has_eligible_instruments"] = bool(eligible)
    if not eligible:
        error(None, "no_eligible_instruments")
    authorized = not errors and all(criteria.values()) and len(valid) == len(eligible)
    result = {
        "version": ARENA_QUALITY_VERSION, "verification_scope": "persisted_snapshot_checks",
        "status": "verified" if authorized else "blocked", "authorized": authorized,
        "expected_session": expected, "session_close_at": close.isoformat() if close else None,
        "required_count": len(required),
        "eligible_count": len(eligible), "valid_count": len(valid),
        "price_ready": bool(required and criteria["all_current_prices"] and criteria["all_calendar_verified"]
                            and criteria["all_snapshot_evidence"] and criteria["expected_session_closed"]),
        "criteria": criteria, "favorite_tickers": favorites, "eligible_tickers": sorted(eligible),
        "explicit_exclusions": [{"ticker": ticker, "reason": reason} for ticker, reason in sorted(explicit.items())],
        "excluded": sorted(excluded, key=lambda item: item["ticker"]),
        "errors": sorted(errors, key=lambda item: (item["ticker"] or "", item["code"])),
        "evidence": sorted(evidence, key=lambda item: item["ticker"]),
    }
    result["manifest_id"] = hashlib.sha256(_canonical(result).encode("utf-8")).hexdigest()
    return result


def arena_report_authorized(report: Mapping[str, object], *, expected_session=None, owner=None,
                            universe_policy=None) -> bool:
    """Reject legacy/tampered reports and recheck the complete evidence contract."""
    try:
        if not isinstance(report, Mapping) or not isinstance(report.get("rows"), list):
            return False
        payload = {key: value for key, value in report.items() if key != "report_id"}
        if hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest() != report.get("report_id"):
            return False
        manifest = report.get("arena_quality")
        if (not isinstance(manifest, Mapping) or manifest.get("version") != ARENA_QUALITY_VERSION
                or manifest.get("authorized") is not True
                or expected_session is not None and manifest.get("expected_session") != _day(expected_session)
                or manifest.get("expected_session") != report.get("as_of")):
            return False
        expected_policy = None
        if universe_policy is not None:
            from src.arena_universe_policy import normalize_arena_universe_policy

            expected_policy = normalize_arena_universe_policy(universe_policy, owner=owner)
            if (expected_policy["mode"] == "verified_only") != ("arena_universe" in report):
                return False
        if "arena_policy_context" in report:
            from src.arena_universe_policy import normalize_arena_universe_policy

            context = report["arena_policy_context"]
            if not isinstance(context, Mapping) or "arena_universe" in report:
                return False
            policy = normalize_arena_universe_policy(context,
                owner=owner if owner is not None else context.get("owner"))
            if (policy["mode"] != "strict" or policy["approved_at"] is None
                    or _canonical(dict(context)) != _canonical(policy)
                    or expected_policy is not None and policy != expected_policy):
                return False
        if "arena_universe" in report:
            from src.arena_universe import resolve_arena_universe

            audit = report["arena_universe"]
            if not isinstance(audit, Mapping) or audit.get("mode") != "verified_only":
                return False
            policy = audit.get("policy")
            if not isinstance(policy, Mapping):
                return False
            rebuilt, expected_audit = resolve_arena_universe(report["rows"],
                favorite_tickers=manifest.get("favorite_tickers"), expected_session=report.get("as_of"),
                policy=policy, owner=owner if owner is not None else policy.get("owner"))
            return (dict(manifest) == rebuilt and _canonical(dict(audit)) == _canonical(expected_audit)
                    and (expected_policy is None or expected_audit["policy"] == expected_policy)
                    and rebuilt["authorized"] is True)
        exclusions = {item["ticker"]: item["reason"] for item in manifest.get("explicit_exclusions", [])}
        rebuilt = build_arena_quality_manifest(report["rows"],
            favorite_tickers=manifest.get("favorite_tickers"), expected_session=report.get("as_of"), exclusions=exclusions)
        return dict(manifest) == rebuilt and rebuilt["authorized"] is True
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
