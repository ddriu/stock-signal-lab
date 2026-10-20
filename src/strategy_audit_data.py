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


AUDIT_DATA_VERSION = "strategy-audit-data-v2"
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
_ISSUE_COLUMNS = ("code", "severity", "ticker", "session", "detail")
_BAR_COLUMNS = (
    "open_at", "close_at", "open", "high", "low", "close", "volume",
    "fx_open", "fx_close", "reduce_setup", "sell_setup", "sma_long",
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


def normalize_audit_signals(
    snapshots: pd.DataFrame | Iterable[Mapping[str, object]],
    source: str = "technical",
) -> AuditSignalSet:
    """Normalize the entire historical universe, retaining first observations.

    ``entry_*`` is the selected source. Technical originals and primary growth
    policy values remain separately inspectable. Revisions never backdate the
    selected observation and are retained with their complete source records.
    Naive creation timestamps are rejected rather than guessing a timezone.
    """

    if source not in {"technical", "growth"}:
        raise ValueError("source must be 'technical' or 'growth'")
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
        event_id = _digest({"source": source, "record": row})
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
        candidates.append(result)
        raw_by_event[event_id] = row

    candidates.sort(key=lambda row: (row["observed_at"], _id_key(row["id"]), row["event_id"]))
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
            kept.append(row)
    signals = pd.DataFrame(kept, columns=_SIGNAL_COLUMNS)
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
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Prepare causal bars; FX quotes are USD per principal native currency.

    Schedule labels identify exchange sessions; its open/close timestamps must
    already have explicit timezones. Unknown FX stays NaN. A bad bar quarantines
    that session and its suffix, never erasing otherwise valid earlier history.
    ``adjustment_basis`` should declare split_adjusted/total_return_adjusted or
    raw/unadjusted. Missing metadata is a limitation, not invented adjustment.
    """

    config.validate()
    symbol = resolve_analysis_ticker(ticker)
    issues: list[dict[str, object]] = []
    quarantines: list[pd.Timestamp] = []
    blocked = False
    attrs = dict(raw.attrs)

    def issue(code: str, severity: str, session: object, detail: str) -> None:
        issues.append(dict(code=code, severity=severity, ticker=symbol, session=session, detail=detail))

    def finish(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        frame = frame.copy()
        frame.attrs.update(attrs)
        frame.attrs.update({"ticker": symbol, "audit_data_version": AUDIT_DATA_VERSION,
                            "strategy_config": asdict(config),
                            "strategy_config_sha256": _digest(asdict(config)),
                            "audit_blocked": blocked or (bool(quarantines) and frame.empty),
                            "audit_has_quarantine": bool(quarantines),
                            "quarantined_from": min(quarantines).isoformat() if quarantines else None,
                            "limitations": tuple(dict.fromkeys(
                                item["code"] for item in issues if item["severity"] != "info"))})
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
    bars["open_at"] = pd.to_datetime([calendar[session][0] for session in bars.index], utc=True)
    bars["close_at"] = pd.to_datetime([calendar[session][1] for session in bars.index], utc=True)
    bars["fx_open"], bars["fx_close"] = np.nan, np.nan
    if not currency_known:
        pass
    elif native_currency == "USD":
        bars["fx_open"], bars["fx_close"] = 1.0, 1.0
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
        if bars[["fx_open", "fx_close"]].isna().any().any():
            issue("fx_unavailable_at_some_bar_times", "warning", None, "No future FX quote was backfilled into an earlier open/close.")
    return finish(bars)
