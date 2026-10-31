"""Read-only, point-in-time inputs for the strategy audit.

An analysis session is not its publication time. Only the explicit, timezone
aware ``created_at`` makes a snapshot observable. Historical position labels
are deliberately absent from the engine-facing output. No provider, database,
current-favorites list, or current fundamentals are consulted by this module.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
import hashlib
import json
import math

import numpy as np
import pandas as pd

from config import StrategyConfig
from src.analysis_policy import decode_policy_note
from src.indicators import add_indicators
from src.instruments import resolve_analysis_ticker
from src.price_units import normalize_price_frame_units, resolve_quote_unit
from src.signal_engine import add_signal_columns


AUDIT_DATA_VERSION = "strategy-audit-data-v3"
VERIFIED_COHORT_VERSION = "recorded-quality-first-observation-v1"
_GROWTH_READINGS = frozenset({
    "Entrada fuerte", "Entrada candidata", "Vigilancia activa", "No preparada",
    "Esperar mejor precio", "Pendiente de fundamentales", "Datos empresariales parciales",
    "Datos empresariales insuficientes", "Datos insuficientes",
    "Lectura de crecimiento pendiente", "Datos de crecimiento incompletos",
})
_SIGNAL_COLUMNS = (
    "event_id", "id", "ticker", "source", "signal_session", "observed_at",
    "entry_label", "entry_score", "label", "score", "technical_entry_label",
    "technical_entry_score", "growth_label", "growth_score", "growth_data_status",
    "growth_entry_reading", "growth_strategy_label",
    "sector", "company_score", "valuation_score", "opportunity_score", "risk_score",
    "relative_score", "confidence_pct", "observed_price", "source_price",
    "policy_price", "price_origin", "reconstructed_price", "currency",
    "config_version", "config_snapshot", "policy_version", "limitations",
)
_VERIFIED_SIGNAL_COLUMNS = (
    "selection_cohort", "raw_event_id", "raw_entry_label", "raw_entry_score", "entry_eligible",
    "entry_eligibility_reason", "quality_observed_status", "quality_observed_as_of",
    "quality_guard_version", "quality_comparability_key", "quality_evidence_hash",
)
_ISSUE_COLUMNS = ("code", "severity", "ticker", "session", "detail")
_BAR_COLUMNS = (
    "open_at", "close_at", "open", "high", "low", "close", "volume",
    "source_close_available_at", "close_available_at",
    "fx_open", "fx_close", "reduce_setup", "sell_setup", "sma_long",
    "exit_indicators_ready", "fx_open_available_at", "fx_close_available_at",
    "fx_open_age_days", "fx_close_age_days",
)


@dataclass(frozen=True)
class AuditSignalSet:
    signals: pd.DataFrame
    rejected: pd.DataFrame
    coverage: dict[str, object]
    manifest: dict[str, object]


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return "" if text.casefold() in {"", "nan", "nat", "none", "<na>"} else text


def _number(value: object) -> float | None:
    if isinstance(value, (bool, np.bool_)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _score(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and 0 <= number <= 100 else None


def _utc(value: object) -> pd.Timestamp | None:
    if value is None or isinstance(value, (bool, int, float, np.number)):
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        return None
    return timestamp.tz_convert("UTC")


def _session(value: object) -> pd.Timestamp | None:
    if value is None or isinstance(value, (bool, int, float, np.number)):
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return None if pd.isna(timestamp) else pd.Timestamp(timestamp.date())


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_json_value(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, (str, bool)) or value is None:
        return value
    number = _number(value)
    return number


def _digest(value: object) -> str:
    payload = json.dumps(_json_value(value), sort_keys=True, ensure_ascii=False,
                         allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _id_key(value: object) -> tuple[int, object]:
    number = _number(value)
    if number is not None:
        return (0, number)
    return (1, _text(value))


def _recorded_entry_gate(policy: Mapping[str, object], primary: Mapping[str, object],
                         session: pd.Timestamp, observed: pd.Timestamp) -> tuple[bool, str]:
    """Read a saved guard, never validate it with today's financial statements.

    Status ``review`` can be an explicitly disclosed annual observation, not an
    account audit. A recorded non-blocking flag alone cannot override missing
    provenance dates, incomplete primary fields or impossible publication clocks.
    """
    if not {"data_status", "entry_candidate"}.issubset(primary):
        return False, "growth_completeness_not_recorded"
    if primary["data_status"] != "complete" or primary["entry_candidate"] is not True:
        return False, "growth_entry_not_complete"
    quality = policy.get("data_quality")
    if not isinstance(quality, Mapping):
        return False, "recorded_quality_missing"
    if quality.get("block_entry") is not False:
        return False, "recorded_quality_blocked"
    if _text(quality.get("status")) not in {"ok", "review"}:
        return False, "recorded_quality_inconsistent"
    if not _text(quality.get("version")):
        return False, "recorded_quality_version_missing"
    as_of = _session(quality.get("as_of"))
    if as_of is None:
        return False, "recorded_quality_as_of_missing"
    if as_of != session:
        return False, "recorded_quality_session_mismatch"
    periods = quality.get("metric_periods")
    for metadata in periods.values() if isinstance(periods, Mapping) else ():
        if not isinstance(metadata, Mapping):
            continue
        filed = _session(metadata.get("filed_at"))
        if filed is not None and filed.date() > observed.date():
            return False, "recorded_quality_published_after_observation"
    return True, "recorded_guard_allows_entry_review"


def normalize_audit_signals(
    snapshots: pd.DataFrame | Iterable[Mapping[str, object]],
    source: str = "technical",
    cohort: str = "raw",
) -> AuditSignalSet:
    """Normalize the entire historical universe, retaining first observations.

    ``entry_*`` is the selected source. Technical originals and primary growth
    policy values remain separately inspectable. Revisions never backdate the
    selected observation and are retained with their complete source records.
    Naive creation timestamps are rejected rather than guessing a timezone.
    ``verified`` is a separate conservative growth cohort: only a quality guard
    saved on that first observation can allow entry. Ineligible observations stay
    visible as non-entries, breaking operational activation without altering the
    raw score/label. Later same-session improvements are deliberately not used,
    even when they preceded the next opening. This is not a replay of today's
    latest-revision presentation policy.
    """

    if source not in {"technical", "growth"}:
        raise ValueError("source must be 'technical' or 'growth'")
    if cohort not in {"raw", "verified"} or cohort == "verified" and source != "growth":
        raise ValueError("cohort must be 'raw', or 'verified' with source='growth'")
    records = (snapshots.to_dict("records") if isinstance(snapshots, pd.DataFrame)
               else [dict(row) for row in snapshots])
    candidates: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    universe: set[str] = set()
    raw_by_event: dict[str, dict[str, object]] = {}

    def reject(row: Mapping[str, object], reason: str, **extra: object) -> None:
        rejected.append({"id": row.get("id"), "ticker": row.get("ticker"),
                         "source": source, "reason": reason,
                         "created_at": row.get("created_at"),
                         "analyzed_at": row.get("analyzed_at"),
                         "raw_record": dict(row), **extra})

    for row in records:
        raw_ticker = _text(row.get("ticker"))
        if not raw_ticker:
            reject(row, "missing_ticker")
            continue
        ticker = resolve_analysis_ticker(raw_ticker)
        universe.add(ticker)
        observed = _utc(row.get("created_at"))
        if observed is None:
            reason = ("missing_created_at" if not _text(row.get("created_at"))
                      and not isinstance(row.get("created_at"), (pd.Timestamp, datetime))
                      else "invalid_or_naive_created_at")
            reject(row, reason)
            continue
        session = _session(row.get("analyzed_at"))
        if session is None:
            reject(row, "missing_or_invalid_signal_session")
            continue
        if session.date() > observed.date():
            reject(row, "signal_session_after_observation")
            continue
        policy = decode_policy_note(row.get("note"))
        policy_ticker = _text(policy.get("ticker")) if policy else ""
        policy_identity_mismatch = bool(policy_ticker and resolve_analysis_ticker(policy_ticker) != ticker)
        if policy_identity_mismatch:
            if source == "growth":
                reject(row, "growth_policy_ticker_mismatch")
                continue
            policy = None
        primary = policy.get("primary", {}) if policy else {}
        technical_label = _text(row.get("entry_label"))
        technical_score = _score(row.get("entry_score"))
        # In the actual policy schema label is the STRATEGY NAME. status is
        # the recorded engine reading; entry_reading is explanatory prose.
        growth_status = _text(primary.get("status"))
        legacy_growth_label = not growth_status and _text(primary.get("label")) in _GROWTH_READINGS
        growth_label = (growth_status if growth_status in _GROWTH_READINGS else
                        _text(primary.get("label")) if legacy_growth_label else "")
        growth_score = _score(primary.get("score"))
        if source == "growth":
            if policy is None:
                reject(row, "missing_or_invalid_growth_policy")
                continue
            strategy_id = _text(primary.get("strategy_id")) or _text(primary.get("id"))
            if strategy_id and strategy_id != "growth_momentum":
                reject(row, "wrong_growth_strategy")
                continue
            label, score = growth_label, growth_score
            if label in {"Entrada fuerte", "Entrada candidata"} and (
                ("data_status" in primary and primary["data_status"] != "complete")
                or ("entry_candidate" in primary and primary["entry_candidate"] is not True)
            ):
                if cohort == "raw":
                    reject(row, "growth_entry_not_complete")
                    continue
            primary_session = _session(primary.get("as_of"))
            if primary_session is not None and primary_session > session:
                reject(row, "growth_policy_after_signal_session")
                continue
        else:
            label, score = technical_label, technical_score
        if not label or score is None:
            reject(row, "missing_or_invalid_entry_reading")
            continue

        config = policy.get("config", {}) if policy else {}
        config = dict(config) if isinstance(config, Mapping) else {}
        config_version = (_text(row.get("config_version"))
                          or _text(config.get("config_version")) or _text(config.get("version")))
        limitations = [] if config_version else ["historical_strategy_config_version_not_recorded"]
        if policy_identity_mismatch:
            limitations.append("unrelated_growth_policy_ignored")
        if legacy_growth_label:
            limitations.append("legacy_growth_label_field_used")
        if source == "growth" and not {"data_status", "entry_candidate"}.issubset(primary):
            limitations.append("growth_completeness_not_recorded")
        raw_price = _number(row.get("price"))
        raw_price = raw_price if raw_price is not None and raw_price > 0 else None
        policy_price = _number(policy.get("price")) if policy else None
        policy_price = policy_price if policy_price is not None and policy_price > 0 else None
        if raw_price is None:
            limitations.append("snapshot_price_not_recorded")
        event_id = _digest({"source": source, "record": row} if cohort == "raw" else
                           {"source": source, "cohort": cohort, "cohort_version": VERIFIED_COHORT_VERSION,
                            "record": row})
        result: dict[str, object] = {
            "event_id": event_id, "id": row.get("id"), "ticker": ticker, "source": source,
            "signal_session": session, "observed_at": observed,
            "entry_label": label, "entry_score": score, "label": label, "score": score,
            "technical_entry_label": technical_label, "technical_entry_score": technical_score,
            "growth_label": growth_label or None, "growth_score": growth_score,
            "growth_data_status": primary.get("data_status"),
            "growth_entry_reading": _text(primary.get("entry_reading")) or None,
            "growth_strategy_label": _text(primary.get("label")) or None,
            "sector": _text(row.get("sector")), "observed_price": raw_price,
            "source_price": raw_price, "policy_price": policy_price,
            "price_origin": "observed_snapshot" if raw_price is not None else "not_recorded",
            "reconstructed_price": None,
            "currency": _text(row.get("currency")) or (_text(policy.get("currency")) if policy else ""),
            "config_version": config_version or None, "config_snapshot": config,
            "policy_version": policy.get("policy_version") if policy else None,
            "limitations": tuple(limitations),
        }
        for column in ("company_score", "valuation_score", "opportunity_score", "risk_score",
                       "relative_score", "confidence_pct"):
            result[column] = _score(row.get(column))
        if cohort == "verified":
            quality = policy.get("data_quality")
            quality = quality if isinstance(quality, Mapping) else {}
            is_entry = label in {"Entrada fuerte", "Entrada candidata"}
            allowed, gate_reason = (_recorded_entry_gate(policy, primary, session, observed)
                                    if is_entry else (False, "not_an_entry"))
            result.update(selection_cohort=cohort, raw_event_id=_digest({"source": source, "record": row}),
                raw_entry_label=label, raw_entry_score=score,
                entry_eligible=allowed, entry_eligibility_reason=gate_reason,
                quality_observed_status=_text(quality.get("status")) or None,
                quality_observed_as_of=_text(quality.get("as_of")) or None,
                quality_guard_version=_text(quality.get("version")) or None,
                quality_comparability_key=_text(quality.get("comparability_key")) or None,
                quality_evidence_hash=_digest(quality) if quality else None)
            if is_entry and not allowed:
                result.update(entry_label="Sin entrada verificada", label="Sin entrada verificada",
                    limitations=(*result["limitations"], gate_reason))
        candidates.append(result)
        raw_by_event[event_id] = row

    candidates.sort(key=lambda row: (row["observed_at"], _id_key(row["id"]), row["event_id"]))
    ambiguous_first: set[tuple[object, object]] = set()
    if cohort == "verified":
        first_clocks: dict[tuple[object, object], tuple[object, object]] = {}
        tied_ids: dict[tuple[object, object], set[str]] = {}
        for row in candidates:
            key = (row["ticker"], row["signal_session"])
            clock = (row["observed_at"], _id_key(row["id"]))
            first_clocks.setdefault(key, clock)
            if clock == first_clocks[key]:
                tied_ids.setdefault(key, set()).add(str(row["event_id"]))
        ambiguous_first = {key for key, identities in tied_ids.items() if len(identities) > 1}
    kept: list[dict[str, object]] = []
    first: dict[tuple[object, object], str] = {}
    for row in candidates:
        key = (row["ticker"], row["signal_session"])
        if key in first:
            reject(raw_by_event[str(row["event_id"])], "later_revision",
                   event_id=row["event_id"], first_event_id=first[key],
                   observed_at=row["observed_at"], signal_session=row["signal_session"])
        else:
            first[key] = str(row["event_id"])
            if key in ambiguous_first and row["raw_entry_label"] in {"Entrada fuerte", "Entrada candidata"}:
                row.update(entry_label="Sin entrada verificada", label="Sin entrada verificada",
                    entry_eligible=False, entry_eligibility_reason="recorded_first_observation_ambiguous",
                    limitations=(*row["limitations"], "recorded_first_observation_ambiguous"))
            kept.append(row)
    signals = pd.DataFrame(kept, columns=(*_SIGNAL_COLUMNS,
        *(_VERIFIED_SIGNAL_COLUMNS if cohort == "verified" else ())))
    signals["observed_at"] = pd.to_datetime(signals["observed_at"], utc=True)
    signals["signal_session"] = pd.to_datetime(signals["signal_session"])
    rejections = pd.DataFrame(rejected, columns=list(dict.fromkeys(
        ["id", "ticker", "source", "reason", "created_at", "analyzed_at", "raw_record",
         "event_id", "first_event_id", "observed_at", "signal_session"]
    )))
    coverage: dict[str, object] = {
        "source": source, "input_rows": len(records), "accepted_signals": len(signals),
        "rejected_rows": len(rejections), "historical_tickers": len(universe),
        "universe": sorted(universe), "signal_sessions": signals["signal_session"].nunique(),
        "rejection_reasons": rejections["reason"].value_counts().to_dict(),
    }
    manifest: dict[str, object] = {
        "data_version": AUDIT_DATA_VERSION, "source": source,
        "input_sha256": _digest(sorted(_digest(row) for row in records)),
        "universe": sorted(universe), "observation_clock": "created_at",
        "observation_timezone": "UTC (explicit source offset required)",
        "deduplication": "first created_at, then id, per ticker/session/source",
        "historical_position_labels_used": False,
        "limitations": ["created_at_is_client_generated_not_independently_attested",
                        "historical_universe_is_observed_not_survivorship_free",
                        *sorted({item for row in kept for item in row["limitations"]})],
    }
    if cohort == "verified":
        mask = signals["raw_entry_label"].isin({"Entrada fuerte", "Entrada candidata"})
        eligible = signals["entry_eligible"].eq(True)
        coverage.update(cohort=cohort, entry_observations=int(mask.sum()),
            verified_entry_observations=int((mask & eligible).sum()),
            blocked_entry_observations=int((mask & ~eligible).sum()),
            blocked_entry_reasons=signals.loc[mask & ~eligible, "entry_eligibility_reason"].value_counts().to_dict())
        manifest.update(cohort=cohort, cohort_version=VERIFIED_COHORT_VERSION,
            quality_source="Only the quality guard saved in each first observed snapshot; no current fundamentals or recomputation.",
            entry_selection="Recorded entry plus complete primary and non-blocking dated saved quality guard; blocked observations remain non-entries.",
            cohort_observations_sha256=_digest(kept))
        manifest["cohort_limitations"] = [
            "Later same-session revisions are ignored even when available before the next opening; not an exact replay of latest-revision operations.",
            "Saved non-blocking review status may describe annual data; it is not independently audited financial information.",
            "Conflicting first records tied on observation time and ID cannot confirm an entry; the content hash is not a publication-order clock.",
        ]
        manifest["limitations"].extend(manifest["cohort_limitations"])
    return AuditSignalSet(signals, rejections, coverage, manifest)


def prepare_audit_bars(
    raw: pd.DataFrame,
    ticker: str,
    currency: str,
    schedule: pd.DataFrame,
    fx: pd.DataFrame | None,
    config: StrategyConfig,
    *,
    actions: pd.DataFrame | None = None,
    splits: pd.Series | pd.DataFrame | Mapping[object, object] | None = None,
    fx_max_age_days: float = 7.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Prepare causal bars; FX quotes are USD per principal native currency.

    Schedule labels identify exchange sessions; its open/close timestamps must
    already have explicit timezones. Unknown FX stays NaN. A bad bar quarantines
    that session and its suffix, never erasing otherwise valid earlier history.
    ``adjustment_basis`` should declare split_adjusted/total_return_adjusted or
    raw/unadjusted. Missing metadata is a limitation, not invented adjustment.
    ``fx_max_age_days`` is an explicit data-quality limit, not a strategy
    parameter. Stale quotes retain their availability/age but cannot value or
    execute a lot. Unready exit indicators remain unknown, never false or sell.
    Explicit ``close_available_at`` clocks must follow the scheduled close.
    ``source_close_available_at`` preserves each supplied clock; the effective
    ``close_available_at`` is its cumulative knowledge maximum, because an
    indicator can depend on previous bars that arrived late. Missing explicit
    clocks leave that bar and dependent suffix unavailable. If no clocks are
    supplied, exchange closes are only a declared retrospective proxy, never
    proof that a prospective collector observed the data at that time.
    """

    config.validate()
    maximum_fx_age = _number(fx_max_age_days)
    if maximum_fx_age is None or maximum_fx_age <= 0:
        raise ValueError("fx_max_age_days must be finite and positive")
    symbol = resolve_analysis_ticker(ticker)
    issues: list[dict[str, object]] = []
    quarantines: list[pd.Timestamp] = []
    blocked = False
    attrs = dict(raw.attrs)
    has_close_availability = "close_available_at" in raw.columns
    attrs.update(close_availability_assumed=not has_close_availability,
                 close_availability_basis=("explicit_source_clock_cumulative_knowledge"
                     if has_close_availability else "historical_exchange_close_proxy"))

    def issue(code: str, severity: str, session: object, detail: str) -> None:
        issues.append(dict(code=code, severity=severity, ticker=symbol, session=session, detail=detail))

    def finish(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        frame = frame.copy()
        frame.attrs.update(attrs)
        frame.attrs.update({"ticker": symbol, "audit_data_version": AUDIT_DATA_VERSION,
                            "strategy_config": asdict(config),
                            "strategy_config_sha256": _digest(asdict(config)),
                            "fx_max_age_days": maximum_fx_age,
                            "fx_staleness_policy": "Elapsed calendar days since the quote became available; stale rates are unavailable, not forward-filled.",
                            "exit_readiness_policy": "Exit flags are unknown until score inputs and the configured trend-confirmation history are available.",
                            "close_availability_policy": "Explicit source clocks are preserved; the effective clock includes all earlier indicator inputs. Without explicit clocks, exchange close is a retrospective assumption, not an observed feed timestamp.",
                            "audit_blocked": blocked or (bool(quarantines) and frame.empty),
                            "audit_has_quarantine": bool(quarantines),
                            "quarantined_from": min(quarantines).isoformat() if quarantines else None,
                            "limitations": tuple(dict.fromkeys([
                                *(["historical_close_availability_assumed"] if not has_close_availability else []),
                                *(item["code"] for item in issues if item["severity"] != "info")]))})
        return frame, pd.DataFrame(issues, columns=_ISSUE_COLUMNS)

    empty = pd.DataFrame(columns=_BAR_COLUMNS, index=pd.DatetimeIndex([], name="session"))
    if raw.empty:
        issue("empty_price_history", "error", None, "No OHLCV history supplied.")
        blocked = True
        return finish(empty)
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(raw.columns):
        issue("missing_ohlcv", "error", None, str(sorted(required.difference(raw.columns))))
        blocked = True
        return finish(empty)
    if not {"market_open", "market_close"}.issubset(schedule.columns):
        issue("invalid_schedule", "error", None, "Authoritative open and close timestamps required.")
        blocked = True
        return finish(empty)

    calendar: dict[pd.Timestamp, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for label, row in schedule.iterrows():
        session = _session(label)
        opened, closed = _utc(row["market_open"]), _utc(row["market_close"])
        if session is None or opened is None or closed is None or opened >= closed:
            issue("invalid_schedule_timestamp", "error", session, "Schedule times must be timezone-aware and ordered.")
            blocked = True
            continue
        if session in calendar and calendar[session] != (opened, closed):
            issue("discordant_duplicate_schedule", "error", session, "Conflicting session times.")
            quarantines.append(session)
        calendar[session] = (opened, closed)
    if not calendar:
        issue("empty_schedule", "error", None, "No authoritative trading sessions supplied.")
        blocked = True
    if blocked:
        return finish(empty)

    unit = resolve_quote_unit(currency)
    normalized = normalize_price_frame_units(raw.loc[:, ["open", "high", "low", "close", "volume"]], currency)
    attrs.update(normalized.attrs)
    native_currency = _text(normalized.attrs.get("display_currency")) or unit.display_currency
    currency_known = len(native_currency) == 3 and native_currency.isalpha()
    if not currency_known:
        issue("unknown_currency", "error", None, "Native currency must be explicit; USD is never assumed.")
        blocked = True
    normalized.index = pd.DatetimeIndex([_session(value) for value in normalized.index], name="session")
    if normalized.index.isna().any():
        issue("invalid_price_session", "error", None, "Cannot assign a daily price row to a session.")
        blocked = True
        return finish(empty)
    normalized = normalized.sort_index(kind="stable")
    for column in required:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    for session, duplicates in normalized.groupby(level=0, sort=False):
        if len(duplicates) > 1:
            discordant = any(duplicates[column].nunique(dropna=False) > 1 for column in required)
            if discordant:
                issue("discordant_duplicate_ohlcv", "error", session, "No arbitrary duplicate price revision selected.")
                quarantines.append(session)
            else:
                issue("identical_duplicate_ohlcv", "info", session, "Identical rows deduplicated.")
    normalized = normalized.loc[~normalized.index.duplicated(keep="first")]
    values = normalized[list(required)].to_numpy(dtype=float)
    valid = np.isfinite(values).all(axis=1)
    valid &= (normalized[["open", "high", "low", "close"]] > 0).all(axis=1).to_numpy()
    valid &= (normalized["volume"] >= 0).to_numpy()
    valid &= (normalized["low"] <= normalized[["open", "close"]].min(axis=1)).to_numpy()
    valid &= (normalized["high"] >= normalized[["open", "close"]].max(axis=1)).to_numpy()
    for session in normalized.index[~valid]:
        issue("invalid_ohlcv", "error", session, "Non-finite, non-positive, or inconsistent price bar.")
        quarantines.append(session)

    basis = _text(attrs.get("adjustment_basis")).casefold()
    adjusted = basis in {"split_adjusted", "split_and_dividend_adjusted", "total_return_adjusted",
                         "total_adjusted", "adjusted", "yahoo_auto_adjust"}
    if not basis:
        issue("adjustment_basis_not_recorded", "warning", None, "Corporate-action consistency cannot be independently verified.")
    split_events: list[tuple[object, object]] = []
    for event_data in (actions, splits):
        if isinstance(event_data, pd.DataFrame):
            column = next((name for name in ("split_ratio", "Stock Splits", "stock_splits", "splits", "split", "ratio") if name in event_data), None)
            if column is not None:
                split_events.extend(event_data[column].items())
        elif isinstance(event_data, (pd.Series, Mapping)):
            split_events.extend(event_data.items())
    for label, value in split_events:
        session, ratio = _session(label), _number(value)
        if session is not None and normalized.index.min() <= session <= normalized.index.max() and ratio not in {None, 0.0, 1.0}:
            if not adjusted:
                issue("unadjusted_split_not_supported", "error", session, "Requires a coherent adjusted series; audit will not invent share changes.")
                quarantines.append(session)
            else:
                issue("split_adjusted_series_declared", "info", session, "Declared coherent adjustment retained without synthetic quantity changes.")
    previous = normalized["close"].shift(1)
    close_ratio = normalized["close"] / previous
    open_ratio = normalized["open"] / previous
    intrabar_ratio = normalized["high"] / normalized["low"]
    suspicious = ((close_ratio >= 90) | (close_ratio <= 1 / 90)
                  | (open_ratio >= 90) | (open_ratio <= 1 / 90) | (intrabar_ratio >= 90))
    for session in normalized.index[suspicious.fillna(False)]:
        issue("possible_100x_unit_change", "error", session, "Unexplained atomic-unit-sized move quarantined, not counted as a real investment loss.")
        quarantines.append(session)
    for session in normalized.index:
        if session not in calendar:
            issue("session_missing_schedule", "error", session, "No trading time is invented for this price row.")
            quarantines.append(session)
    if quarantines:
        normalized = normalized.loc[normalized.index < min(quarantines)]
    if normalized.empty:
        return finish(empty)

    # Discard all previously computed columns: indicators and rule flags must
    # correspond to this frozen config, not to a cached live-account analysis.
    bars = add_signal_columns(add_indicators(normalized, config), config)
    score_inputs = (
        "sma_short", "sma_medium", "sma_long", "sma_medium_slope", "rsi",
        "macd", "macd_signal", "volume_average", "distance_sma_short_pct",
        "momentum_short_pct", "momentum_medium_pct", "distance_high_pct",
    )
    score_ready = bars.loc[:, score_inputs].notna().all(axis=1)
    breakout_ready = (bars["close"].shift(1).rolling(config.breakout_period,
        min_periods=config.breakout_period).count() >= config.breakout_period)
    trend_ready = bars[["sma_medium", "sma_long"]].notna().all(axis=1)
    confirmation_ready = (trend_ready.astype(int).rolling(config.trend_confirmation_days,
        min_periods=config.trend_confirmation_days).sum() >= config.trend_confirmation_days)
    bars["exit_indicators_ready"] = score_ready & breakout_ready & confirmation_ready
    for flag in ("reduce_setup", "sell_setup"):
        bars[flag] = bars[flag].astype("boolean").where(bars["exit_indicators_ready"], pd.NA)
    unready = ~bars["exit_indicators_ready"]
    if unready.any():
        issue("exit_indicators_not_ready", "warning", bars.index[unready][0],
              f"{int(unready.sum())} supplied sessions lack mature exit indicators/confirmation history; exit flags remain unknown.")
    bars["open_at"] = pd.to_datetime([calendar[session][0] for session in bars.index], utc=True)
    bars["close_at"] = pd.to_datetime([calendar[session][1] for session in bars.index], utc=True)
    if has_close_availability:
        source_column = ("source_close_available_at"
                         if "source_close_available_at" in raw.columns else "close_available_at")
        arrivals: dict[pd.Timestamp, list[pd.Timestamp]] = {}
        for label, value in raw[source_column].items():
            session, arrived = _session(label), _utc(value)
            if session in calendar and arrived is not None and arrived >= calendar[session][1]:
                arrivals.setdefault(session, []).append(arrived)
        own_clocks = pd.Series(pd.to_datetime([
            min(arrivals[session]) if session in arrivals else pd.NaT for session in bars.index
        ], utc=True), index=bars.index)
        bars["source_close_available_at"] = own_clocks
        # A missing explicit source clock is unknown, not permission to use an
        # unobserved bar as an input to later recursive indicators.
        bars["close_available_at"] = own_clocks.cummax().where(own_clocks.notna().cummin())
        invalid = own_clocks.isna()
        if invalid.any():
            issue("missing_or_invalid_close_availability", "warning", bars.index[invalid][0],
                  f"{int(invalid.sum())} supplied close clocks are absent, naive, or earlier than the final exchange close; dependent history remains unavailable.")
    else:
        bars["source_close_available_at"] = pd.Series(pd.NaT, index=bars.index, dtype="datetime64[ns, UTC]")
        bars["close_available_at"] = bars["close_at"]
    bars["fx_open"], bars["fx_close"] = np.nan, np.nan
    for field in ("open", "close"):
        bars[f"fx_{field}_available_at"] = pd.Series(pd.NaT, index=bars.index, dtype="datetime64[ns, UTC]")
        bars[f"fx_{field}_age_days"] = np.nan
    if not currency_known:
        pass
    elif native_currency == "USD":
        bars["fx_open"], bars["fx_close"] = 1.0, 1.0
        for field in ("open", "close"):
            bars[f"fx_{field}_available_at"] = bars[f"{field}_at"]
            bars[f"fx_{field}_age_days"] = 0.0
    elif fx is None or fx.empty or not {"close", "available_at"}.issubset(fx.columns):
        issue("missing_point_in_time_fx", "warning", None, "USD-per-native quotes with available_at are required for valuation/fills.")
    else:
        fx_records = []
        for _, row in fx.iterrows():
            available, rate = _utc(row["available_at"]), _number(row["close"])
            if available is None or rate is None or rate <= 0:
                issue("invalid_fx_observation", "warning", None, "Invalid or naive FX availability/rate ignored.")
            else:
                fx_records.append({"available_at": available, "rate": rate})
        if fx_records:
            rates = pd.DataFrame(fx_records).sort_values("available_at", kind="stable")
            for available, duplicates in rates.groupby("available_at"):
                if duplicates["rate"].nunique() > 1:
                    issue("discordant_duplicate_fx", "error", None, f"Conflicting rates available at {available.isoformat()} ignored.")
                    rates = rates.loc[rates["available_at"] != available]
            rates = rates.drop_duplicates("available_at", keep="first")
            if not rates.empty:
                for field, output in (("open_at", "fx_open"), ("close_at", "fx_close")):
                    targets = pd.DataFrame({"at": bars[field].array, "order": np.arange(len(bars))}).sort_values("at")
                    matched = pd.merge_asof(targets, rates, left_on="at", right_on="available_at", direction="backward", allow_exact_matches=True)
                    matched = matched.sort_values("order")
                    bars[output] = matched["rate"].to_numpy()
                    bars[f"{output}_available_at"] = pd.to_datetime(matched["available_at"].array, utc=True)
                    age = (pd.to_datetime(matched["at"], utc=True)
                           - pd.to_datetime(matched["available_at"], utc=True)).dt.total_seconds() / 86400.0
                    bars[f"{output}_age_days"] = age.to_numpy()
                    stale = bars[f"{output}_age_days"] > maximum_fx_age
                    if stale.any():
                        bars.loc[stale, output] = np.nan
                        issue("stale_fx_observation", "warning", bars.index[stale][0],
                              f"{int(stale.sum())} {output} marks exceed {maximum_fx_age:g} elapsed calendar days; availability and age retained but rates are unavailable.")
        if bars[["fx_open", "fx_close"]].isna().any().any():
            issue("fx_unavailable_at_some_bar_times", "warning", None, "No future FX quote was backfilled into an earlier open/close.")
    return finish(bars)
