"""Offline three-way execution experiment around the frozen A/B/C/D models.

Recorded entry labels are not recomputed using present-day fundamentals. Every
observation is retained, including non-entries, so activation state is explicit.
The filtered variants activate on their effective (filtered) strong-entry state;
this is not claimed to be the same activation cohort as the raw baseline.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
import hashlib
from types import MappingProxyType

import pandas as pd

from src.data_sources import FxSnapshot
from src.growth_execution_review import ExecutionReviewConfig, review_growth_execution
from src.model_results_engine import ExecutionCosts, ModelResult, run_frozen_models
from src.model_results_service import _benchmark, _matched_hold
from src.price_units import resolve_quote_unit
from src.strategy_audit_data import AuditSignalSet
from src.strategy_audit_engine import _boundary, _hash
from src.strategy_audit_service import BENCHMARKS


EXECUTION_COMPARISON_VERSION = "model-execution-comparison-v1"
VARIANTS = ("baseline", "liquidity_eur", "execution_setup")
ENTRY_LABELS = {"Entrada fuerte", "Entrada candidata", "Entrada interesante"}
PREPARED_FX_BASES = frozenset({
    "Prior available daily close; conservative availability next day 02:00 UTC",
    "Conservative daily close available next day 02:00 UTC",
})


@dataclass(frozen=True)
class ExecutionComparisonReport:
    observations: AuditSignalSet
    models: Mapping[str, Mapping[str, ModelResult]]
    signals: Mapping[str, pd.DataFrame]
    decisions: pd.DataFrame
    summary: pd.DataFrame
    daily: pd.DataFrame
    lots: pd.DataFrame
    trades: pd.DataFrame
    benchmarks: pd.DataFrame
    matched_hold: Mapping[str, Mapping[str, ModelResult]]
    issues: pd.DataFrame
    manifest: dict[str, object]


def _utc(value):
    try:
        timestamp = pd.Timestamp(value)
        return timestamp.tz_convert("UTC") if pd.notna(timestamp) and timestamp.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError): return None


def _day(value):
    try:
        timestamp = pd.Timestamp(value)
        return timestamp.date() if pd.notna(timestamp) else None
    except (ValueError, TypeError, OverflowError): return None


def _fx_records(source):
    """USD per principal native unit, with explicit economic/publication clocks."""
    result = {}
    for currency, frame in (source or {}).items():
        if not isinstance(frame, pd.DataFrame) or not {"close", "available_at", "economic_day"}.issubset(frame):
            continue
        accepted = []
        for row in frame.to_dict("records"):
            available, economic = _utc(row.get("available_at")), _day(row.get("economic_day"))
            if isinstance(row.get("close"), bool): continue
            try: rate = float(row.get("close"))
            except (ValueError, TypeError, OverflowError): continue
            if (available is None or economic is None or available.date() < economic
                    or not 0 < rate < float("inf")):
                continue
            accepted.append(dict(available_at=available, economic_day=economic, close=rate))
        clean = pd.DataFrame(accepted)
        if clean.empty: continue
        # Never choose arbitrarily between contradictory quotes at one clock.
        bad = clean.groupby(["economic_day", "available_at"]).close.transform("nunique").gt(1)
        clean = clean.loc[~bad].drop_duplicates().sort_values(["economic_day", "available_at"])
        result[str(currency).strip().upper()] = clean.to_dict("records")
    return result


def _execution_fx(currency, session, known_at, records):
    if currency == "EUR": return None, None
    euro = {row["economic_day"]: row for row in records.get("EUR", [])
            if row["economic_day"] <= session and row["available_at"] <= known_at}
    if currency == "USD":
        if not euro: return None, None
        day = max(euro)
        rate = euro[day]
        return FxSnapshot(day, {"EUR": 1., "USD": rate["close"]}), rate["available_at"]
    native = {row["economic_day"]: row for row in records.get(currency, [])
              if row["economic_day"] <= session and row["available_at"] <= known_at}
    common = set(euro) & set(native)
    if not common: return None, None
    day = max(common)
    eur, local = euro[day], native[day]
    return (FxSnapshot(day, {"EUR": 1., currency: eur["close"] / local["close"], "USD": eur["close"]}),
            max(eur["available_at"], local["available_at"]))


def fx_records_from_prepared_bars(bars: Mapping[str, pd.DataFrame], *, fx_basis: str = "") -> dict[str, pd.DataFrame]:
    """Recover only explicitly recorded FX clocks from a frozen prepared cache.

    An economic day is never derived from a stock session. Without an explicit
    fx_close_economic_day column, inversion of the *declared* next-day-02UTC
    acquisition protocol is permitted only for exact 02:00UTC clocks. Unknown
    protocols return no inferred quotes; this helper does not fetch anything.
    """
    candidates = {}
    for frame in bars.values():
        currency = resolve_quote_unit(frame.attrs.get("display_currency") or frame.attrs.get("currency") or "").display_currency
        if currency == "USD" or not currency or not {"fx_close", "fx_close_available_at"}.issubset(frame):
            continue
        explicit = "fx_close_economic_day" in frame
        if not explicit and fx_basis not in PREPARED_FX_BASES:
            continue
        for row in frame.to_dict("records"):
            available = _utc(row.get("fx_close_available_at"))
            if available is None: continue
            if explicit:
                economic = _day(row.get("fx_close_economic_day"))
            else:
                if (available.hour, available.minute, available.second, available.microsecond, available.nanosecond) != (2, 0, 0, 0, 0):
                    continue
                economic = (available - pd.Timedelta(days=1, hours=2)).date()
            if economic is None: continue
            candidates.setdefault(currency, []).append({"close": row.get("fx_close"),
                "economic_day": economic, "available_at": available})
    cleaned = _fx_records({currency: pd.DataFrame(rows) for currency, rows in candidates.items()})
    output = {currency: pd.DataFrame(rows) for currency, rows in cleaned.items() if rows}
    for frame in output.values():
        frame.attrs["fx_basis"] = fx_basis
        frame.attrs["economic_day_inference"] = "Explicit field where present; otherwise declared next-day-02UTC protocol, not stock-session date"
    return output


def _combined(tables):
    present = [frame.assign(variant=variant, model=model)
               for variant, models in tables.items() for model, frame in models.items() if not frame.empty]
    return pd.concat(present, ignore_index=True) if present else pd.DataFrame()


def build_execution_comparison(observations: AuditSignalSet, bars: Mapping[str, pd.DataFrame], *,
        raw_frames: Mapping[str, pd.DataFrame] | None = None, metadata: Mapping[str, object] | None = None,
        fx_records: Mapping[str, pd.DataFrame] | None = None, start, end, budget_usd: float = 6000.,
        costs: ExecutionCosts | None = None, git_sha: str = "unknown",
        execution_config: ExecutionReviewConfig | None = None,
        benchmark_bars: Mapping[str, pd.DataFrame] | None = None,
        include_matched_hold: bool = False) -> ExecutionComparisonReport:
    """Same fixed USD budget, assumptions and unchanged frozen engine in all arms.

    fx_records maps native currency to USD-per-native rows with close,
    economic_day and available_at. EUR is the USD/EUR series. Independent EUR
    liquidity reviews require quotes of the same economic day, known before the
    recorded observation; no current FX or company data is backfilled.
    """
    if not isinstance(observations, AuditSignalSet): raise TypeError("AuditSignalSet required")
    if not bars: raise ValueError("Prepared causal execution bars are required")
    finish = _boundary(end, True)
    begin = _boundary(start, False)
    if begin is None or finish is None or begin > finish: raise ValueError("Explicit ordered range required")
    config = execution_config or ExecutionReviewConfig()
    config.validate()
    charges = costs or ExecutionCosts()
    charges.validate()
    source = observations.signals.copy(deep=True)
    needed = {"event_id", "ticker", "signal_session", "observed_at", "entry_label", "entry_score"}
    if not needed.issubset(source): raise ValueError("Normalized recorded signal fields required")
    originals = source.to_dict("records")
    prepared_fx = _fx_records(fx_records)
    histories = raw_frames if raw_frames is not None else bars
    logs, liquidity, setup = [], [], []
    for row in originals:
        ticker = str(row.get("ticker") or "").strip().upper()
        observed, session = _utc(row.get("observed_at")), _day(row.get("signal_session"))
        raw_entry = row.get("entry_label") in ENTRY_LABELS
        if observed is not None and observed > finish:
            # The engine ignores post-cutoff observations; do not inspect their
            # later prices or let their context alter the historical decision log.
            liquidity.append(True); setup.append(True)
            continue
        frame = histories.get(ticker, pd.DataFrame())
        item = (metadata or {}).get(ticker, {})
        item = dict(item) if isinstance(item, Mapping) else {"currency": item} if isinstance(item, str) else {}
        # Only quote metadata is consumed; no present-day fundamentals enter a
        # historical recorded signal or its original score.
        currency = str(item.get("currency") or row.get("currency")
            or frame.attrs.get("display_currency") or frame.attrs.get("currency") or "")
        info = {"symbol": ticker, "currency": currency}
        principal = resolve_quote_unit(frame.attrs.get("display_currency") or currency).display_currency
        fx, available = _execution_fx(principal, session, observed, prepared_fx) if session and observed else (None, None)
        if raw_entry:
            context = review_growth_execution({"ticker": ticker, "score": row.get("entry_score"),
                "label": row.get("entry_label"), "as_of": row.get("signal_session")}, info, frame, fx,
                as_of=row.get("signal_session"), known_at=row.get("observed_at"),
                fx_available_at=available, config=config)
        else:
            context = {"status": "not_entry", "setup": "not_evaluated", "adv_eur": None,
                "entry_setup_eligible": False, "reasons": ["Observación no entrante conservada para el estado de activación."],
                "currency": principal or None}
        adv = context.get("adv_eur")
        liquid = bool(raw_entry and adv is not None and adv >= config.min_turnover_eur)
        filtered = bool(raw_entry and context.get("entry_setup_eligible"))
        liquidity.append(liquid); setup.append(filtered)
        logs.append({"event_id": row.get("event_id"), "ticker": ticker, "signal_session": row.get("signal_session"),
            "observed_at": row.get("observed_at"), "original_entry_label": row.get("entry_label"),
            "original_entry_score": row.get("entry_score"), "eligible_baseline": raw_entry,
            "liquidity_eur_eligible": liquid, "entry_setup_eligible": filtered, **context})
    frames = {"baseline": source.copy(deep=True)}
    for variant, allowed in (("liquidity_eur", liquidity), ("execution_setup", setup)):
        frame = source.copy(deep=True)
        frame["execution_raw_entry_label"] = frame["entry_label"]
        frame["execution_raw_entry_score"] = frame["entry_score"]
        frame["execution_entry_eligible"] = allowed
        mask = frame["entry_label"].isin(ENTRY_LABELS) & ~frame["execution_entry_eligible"]
        frame.loc[mask, "entry_label"] = "Sin entrada experimental"
        if "label" in frame: frame.loc[mask, "label"] = "Sin entrada experimental"
        frames[variant] = frame
    models = {variant: run_frozen_models(frame, bars, start=start, end=end, mode="budget",
        budget_usd=budget_usd, costs=charges, git_sha=git_sha) for variant, frame in frames.items()}
    summary = pd.DataFrame([{"variant": variant, "model": model, **{key: value for key, value in result.metrics.items()
        if value is None or isinstance(value, (str, bool, int, float))}}
        for variant, group in models.items() for model, result in group.items()])
    daily = _combined({variant: {model: result.daily.reset_index().rename(columns={"index": "date"})
        for model, result in group.items()} for variant, group in models.items()})
    lots = _combined({variant: {model: result.lots for model, result in group.items()} for variant, group in models.items()})
    trades = _combined({variant: {model: result.trades for model, result in group.items()} for variant, group in models.items()})
    issues = _combined({variant: {model: result.issues for model, result in group.items()} for variant, group in models.items()})
    references = []
    if benchmark_bars is not None:
        for variant, group in models.items():
            for model, result in group.items():
                for name, ticker in BENCHMARKS.items():
                    references.append({"variant": variant, "model": model, "benchmark": ticker, "benchmark_name": name,
                        **_benchmark(result, benchmark_bars.get(ticker, pd.DataFrame()), mode="budget",
                            start=start, end=end, budget_usd=budget_usd, costs=charges)})
    held = {variant: {model: _matched_hold(frames[variant], bars, result, mode="budget", start=start, end=end,
        budget_usd=budget_usd, costs=charges, git_sha=git_sha) for model, result in group.items() if model in {"A", "B", "C"}}
        for variant, group in models.items()} if include_matched_hold else {}
    root = Path(__file__).resolve().parents[1]
    manifest = {"version": EXECUTION_COMPARISON_VERSION, "git_sha": git_sha, "start": str(start), "end": str(end),
        "mode": "budget", "budget_usd": budget_usd, "costs": asdict(charges), "preset": asdict(config),
        "source": observations.manifest, "coverage": observations.coverage,
        "models": {variant: {model: result.manifest for model, result in group.items()} for variant, group in models.items()},
        "source_hashes": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in (
            "src/growth_execution_review.py", "src/model_execution_comparison.py")},
        "context_input_hash": _hash({"observations": source,
            "raw_frames": {ticker: {"frame": frame, "attrs": frame.attrs} for ticker, frame in histories.items()},
            "metadata": metadata or {}, "fx_records": fx_records or {}}),
        "variant_signal_hashes": {name: _hash(frame) for name, frame in frames.items()},
        "fx_provenance": {currency: {"basis": frame.attrs.get("fx_basis") or "Explicit economic_day and available_at records",
            "economic_day_inference": frame.attrs.get("economic_day_inference") or "None: explicit input field"}
            for currency, frame in (fx_records or {}).items() if isinstance(frame, pd.DataFrame)},
        "context_coverage": {"observations_reviewed": len(logs),
            "raw_entry_observations": sum(bool(row.get("eligible_baseline")) for row in logs),
            "liquidity_eur_passed": sum(bool(row.get("liquidity_eur_eligible")) for row in logs),
            "execution_setup_passed": sum(bool(row.get("entry_setup_eligible")) for row in logs)},
        "activation_rule": "All observations retained. B/C activate on effective filtered strong state; a newly passed gate can create an experimental activation, not a new raw-engine crossing.",
        "limitations": ["Comparación retrospectiva exploratoria; no selecciona ganador fuera de muestra ni optimiza parámetros.",
            "Motor, stops, tamaños USD100 y reglas A-D intactos; sólo cambian las etiquetas de entrada admitidas por cada experimento.",
            "Presupuesto virtual y costes hipotéticos iguales; no son liquidez real, impuestos ni ejecuciones de broker.",
            "Los snapshots iniciales, datos faltantes y divisas limitan qué conclusiones pueden extraerse.",
            "Los datos actuales de empresas no sustituyen fundamentales históricos ausentes."]}
    manifest["comparison_hash"] = _hash(manifest)
    return ExecutionComparisonReport(observations, MappingProxyType(models), MappingProxyType(frames), pd.DataFrame(logs),
        summary, daily, lots, trades, pd.DataFrame(references), MappingProxyType(held), issues, manifest)
