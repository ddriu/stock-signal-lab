"""Read-only, retrospective four-model reports and private in-memory exports.

No database, broker, mail or market fetch is called by report construction.
Market acquisition remains a separate, explicit caller action.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from io import BytesIO
import hashlib
import json
import math
from pathlib import Path
import re

import pandas as pd

from config import BacktestConfig, StrategyConfig
from src.backtesting import run_backtest
from src.indicators import add_indicators
from src.model_results_engine import ExecutionCosts, ModelResult, _cost_lots, _daily, _metrics, run_frozen_models
from src.signal_engine import add_signal_columns
from src.strategy_audit_data import AUDIT_DATA_VERSION, AuditSignalSet, normalize_audit_signals
from src.strategy_audit_engine import AuditConfig, AuditResult, _boundary, _hash, _safe, run_strategy_audit
from src.strategy_audit_service import BENCHMARKS, matched_benchmark


SERVICE_VERSION = "model-results-service-v1"
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_PRIVATE_KEYS = {"owner", "email", "username", "password", "secret", "token", "api_key", "recorded_by"}


@dataclass(frozen=True)
class ModelResultsReport:
    observations: AuditSignalSet
    theoretical: Mapping[str, ModelResult]
    budget: Mapping[str, ModelResult]
    benchmarks: pd.DataFrame
    original_reference: AuditResult
    original_backtest: pd.DataFrame
    issues: pd.DataFrame
    manifest: dict[str, object]
    matched_hold: Mapping[str, Mapping[str, ModelResult]]
    matched_hold_summary: pd.DataFrame

    @property
    def signals(self) -> pd.DataFrame:
        return self.observations.signals


def public_value(value: object) -> object:
    """Remove account identifiers from on-screen/private-export views."""
    if isinstance(value, Mapping):
        return {str(key): public_value(item) for key, item in value.items()
                if str(key).lower() not in _PRIVATE_KEYS and "email" not in str(key).lower()}
    if isinstance(value, (list, tuple)):
        return [public_value(item) for item in value]
    if isinstance(value, str):
        return _EMAIL.sub("[correo oculto]", value)
    return value


def public_frame(frame: pd.DataFrame) -> pd.DataFrame:
    safe = frame.drop(columns=[name for name in frame if str(name).lower() in _PRIVATE_KEYS
                              or "email" in str(name).lower()], errors="ignore").copy(deep=True)
    for column in safe:
        if safe[column].dtype == object or pd.api.types.is_string_dtype(safe[column]):
            safe[column] = safe[column].map(public_value)
    return safe


def _benchmark(result: ModelResult, frame: pd.DataFrame, *, mode: str, start: object,
               end: object, budget_usd: float, costs: ExecutionCosts) -> dict:
    if mode == "theoretical":
        reference = matched_benchmark(result.lots, frame, end)
        reference.update(external_contributions_usd=reference["contributions_usd"],
            fees_usd=0.0, cash_usd=0.0,
            flow_rule="Mismos USD100 externos y fechas de entrada ejecutadas del modelo; mantener, sin costes.")
        return reference
    # One and only one external contribution, at the model's initial-funding
    # date. Never turn recycled model sale cash into fresh benchmark funding.
    rate = costs.spread_bps / 10_000  # SPY/QQQ/VT are USD instruments.
    notional = max(0.0, (budget_usd - costs.commission_fixed_usd) / (1 + rate))
    if notional == 0:
        return dict(contributions_usd=budget_usd, external_contributions_usd=budget_usd,
            value_usd=budget_usd, profit_usd=0.0, roi_pct=0.0, pending_contributions=0,
            data_complete=True, fees_usd=0.0, cash_usd=budget_usd,
            flow_rule="Único aporte inicial; no alcanza la comisión de compra y se conserva efectivo.")
    flows = pd.DataFrame([dict(entry_at=_boundary(start, False), contribution_usd=notional)])
    reference = matched_benchmark(flows, frame, end)
    value = reference.get("value_usd")
    complete = bool(reference.get("data_complete"))
    reference.update(contributions_usd=budget_usd, external_contributions_usd=budget_usd,
        invested_notional_usd=notional, fees_usd=budget_usd - notional if complete else None,
        cash_usd=0.0 if complete else None,
        profit_usd=value - budget_usd if complete else None,
        roi_pct=(value / budget_usd - 1) * 100 if complete else None,
        flow_rule="Mismo único aporte inicial presupuestario; compra y mantener desde primera apertura disponible, costes estimados de compra. Ningún aporte por ventas del modelo.")
    return reference


def _legacy_reference(bars: Mapping[str, pd.DataFrame], start: object, end: object) -> pd.DataFrame:
    """Original per-asset allocator, kept separate and never rewritten to match."""
    records = []
    strategy, settings = StrategyConfig(), BacktestConfig()
    for ticker, frame in sorted(bars.items()):
        if ticker in BENCHMARKS.values():
            continue
        native_units = dict(currency=frame.attrs.get("display_currency") or frame.attrs.get("currency") or "N/D",
            capital_units="moneda nativa; sin conversión FX", initial_capital_native=settings.initial_capital)
        try:
            # Audit bars deliberately omit most indicator/signal columns. Build
            # them with the unchanged original functions, from OHLCV only;
            # preserve pre-start warmup but never use a post-cutoff close.
            history = frame.loc[pd.to_datetime(frame.index).date <= pd.Timestamp(end).date()].copy()
            if "close_at" in history:
                history = history.loc[pd.to_datetime(history.close_at, utc=True) <= _boundary(end, True)]
            history = history.loc[:, ["open", "high", "low", "close", "volume"]].sort_index()
            calculated = add_signal_columns(add_indicators(history, strategy), strategy)
            selected = calculated.loc[pd.to_datetime(calculated.index).date >= pd.Timestamp(start).date()].copy()
            # Keep the original allocator's minimum-window requirement; do
            # not relax it merely because warmup indicators are now available.
            result = run_backtest(selected, strategy, settings)
            records.append(dict(ticker=ticker, status="Calculado", reason=None, **native_units, **result.metrics))
        except (ValueError, KeyError, TypeError) as error:
            records.append(dict(ticker=ticker, status="N/D", reason=f"{type(error).__name__}: {str(error)[:180]}", **native_units))
    return pd.DataFrame(records)


def _matched_hold(signals: pd.DataFrame, bars: Mapping[str, pd.DataFrame], model: ModelResult, *,
                  mode: str, start: object, end: object, budget_usd: float,
                  costs: ExecutionCosts, git_sha: str) -> ModelResult:
    """Hold precisely the source model's fills; never fund an infeasible cohort."""
    identifiers = set(model.lots.event_id.astype(str))
    selected = signals.loc[signals.event_id.astype(str).isin(identifiers)].copy()
    base = run_strategy_audit(selected, bars, AuditConfig(start=start, end=end,
        entry_mode="daily", amount_usd=100, exit_policy="hold"))
    keys = ["event_id", "entry_at", "quantity", "entry_price_native", "entry_fx", "contribution_usd"]
    cohort = lambda frame: frame.loc[:, keys].sort_values("event_id").reset_index(drop=True)
    same_cohort = cohort(model.lots).equals(cohort(base.lots))
    charges = ExecutionCosts(0, 0, 0) if mode == "theoretical" else costs
    currencies = {str(ticker).strip().upper(): str(frame.attrs.get("display_currency") or
        frame.attrs.get("currency") or "").upper() for ticker, frame in bars.items()}
    lots, trades = _cost_lots(base, charges, currencies)
    required = float(lots.entry_debit_usd.sum())
    shortfall = max(0.0, required - budget_usd) if mode == "budget" else 0.0
    feasible = shortfall <= 1e-9
    valid_reference = same_cohort and feasible
    daily = _daily(base, lots, mode, budget_usd if mode == "budget" else None, charges, hold=True)
    if not daily.empty:
        daily["capital_required_usd"] = daily.invested_capital_usd + daily.cumulative_costs_usd
        daily["cash_shortfall_usd"] = (daily.capital_required_usd - budget_usd).clip(lower=0) if mode == "budget" else 0.0
    issues = base.issues.copy(deep=True)
    if not valid_reference:
        code = "matched_hold_insufficient_capital" if not feasible else "matched_hold_cohort_mismatch"
        issues = pd.concat([issues, pd.DataFrame([dict(code=code, ticker="", event_id="", session="",
            detail=f"Exact hold cohort unavailable; required USD{required:.2f}, shortfall USD{shortfall:.2f}; no loan/top-up simulated.",
            severity="warning")])], ignore_index=True)
        lots["reference_execution_status"] = "not_funded" if not feasible else "cohort_mismatch"
        for name in ("value_usd", "unrealized_pnl_usd", "realized_pnl_usd"):
            lots[name] = None
        lots["data_complete"] = False
        # Negative funding balances are only used to diagnose required capital,
        # never published as a financed portfolio or an achieved return.
        for name in ("cash_usd", "cash_known_usd", "available_cash_usd", "value_usd", "value_known_usd",
                     "open_value_usd", "open_value_known_usd", "realized_pnl_usd", "unrealized_pnl_usd",
                     "net_return_pct", "unit_value_daily_approx", "drawdown_daily_approx_pct"):
            if name in daily:
                daily[name] = None
        daily["valuation_complete"] = False
    else:
        lots["reference_execution_status"] = "funded_counterfactual"
    metrics = _metrics(base, daily, lots, trades, mode, budget_usd if mode == "budget" else None,
                       charges, start, end)
    if not valid_reference:
        for name in ("realized_pnl_known_usd", "unrealized_pnl_known_usd", "fees_usd", "fees_known_usd",
                     "max_simultaneous_invested_capital_usd", "value_known_usd"):
            metrics[name] = None
        metrics["coverage_complete"] = False
    metrics.update(cohort_matches_model=same_cohort, budget_feasible=feasible,
        capital_required_usd=required, cash_shortfall_usd=shortfall,
        reference_requested_entries=len(model.lots),
        reference_executed_entries=len(lots) if valid_reference else 0,
        estimated_entry_costs_required_usd=float(lots.entry_costs_usd.sum()),
        reference_status=("Sin entradas" if model.lots.empty else "Matched hold")
        if valid_reference else "N/D: capital insuficiente o cohorte diferente")
    manifest = dict(base.manifest)
    manifest.update(reference="matched_hold", git_sha=git_sha, mode=mode,
        source_model_run_hash=model.manifest.get("model_run_hash"),
        requested_event_ids=sorted(identifiers), exact_cohort_keys=keys,
        cohort_matches_model=same_cohort, budget_feasible=feasible,
        capital_required_usd=required, cash_shortfall_usd=shortfall,
        costs=asdict(charges), is_counterfactual=True,
        funding_rule="Same external flows as source model; no sales, loans, top-ups or partial cohort. Infeasible budget hold is N/D, not a leveraged simulation.")
    return ModelResult(daily, lots, trades, issues, metrics, manifest)


def build_model_results(snapshots: pd.DataFrame, bars: Mapping[str, pd.DataFrame], *,
                        start: object, end: object, source: str = "technical",
                        cohort: str = "raw",
                        budget_usd: float = 6000.0, costs: ExecutionCosts | None = None,
                        git_sha: str = "unknown", benchmark_bars: Mapping[str, pd.DataFrame] | None = None,
                        market_manifest: Mapping[str, object] | None = None,
                        market_issues: pd.DataFrame | None = None) -> ModelResultsReport:
    """Build separate sources/cohorts; the default raw reference is unchanged."""
    if not bars:
        raise ValueError("No prepared market inputs available; do not present an unexecuted portfolio as a strategy result")
    strategy = asdict(StrategyConfig())
    for ticker, frame in bars.items():
        recorded = frame.attrs.get("strategy_config")
        if recorded is not None and recorded != strategy:
            raise ValueError(f"Frozen strategy configuration mismatch for {ticker}")
        version = frame.attrs.get("audit_data_version")
        if version is not None and version != AUDIT_DATA_VERSION:
            raise ValueError(f"Prepared cache version mismatch for {ticker}; download/reprepare explicitly")
    observed = normalize_audit_signals(snapshots, source=source, cohort=cohort)
    charges = costs or ExecutionCosts()
    theoretical = run_frozen_models(observed.signals, bars, start=start, end=end,
                                   mode="theoretical", git_sha=git_sha)
    budget = run_frozen_models(observed.signals, bars, start=start, end=end, mode="budget",
                              budget_usd=budget_usd, costs=charges, git_sha=git_sha)
    matched_hold = {}
    hold_rows = []
    for mode, models in (("theoretical", theoretical), ("budget", budget)):
        matched_hold[mode] = {}
        for model_id in ("A", "B", "C"):
            model = models[model_id]
            hold = _matched_hold(observed.signals, bars, model, mode=mode, start=start, end=end,
                budget_usd=budget_usd, costs=charges, git_sha=git_sha)
            matched_hold[mode][model_id] = hold
            comparable = (not model.lots.empty and model.metrics.get("coverage_complete")
                and hold.metrics.get("coverage_complete") and hold.metrics["cohort_matches_model"]
                and hold.metrics["budget_feasible"])
            excess = model.metrics["net_return_pct"] - hold.metrics["net_return_pct"] if comparable else None
            hold_rows.append(dict(mode=mode, model=model_id, source=source,
                entries=len(model.lots), cohort_matches_model=hold.metrics["cohort_matches_model"],
                budget_feasible=hold.metrics["budget_feasible"],
                capital_required_usd=hold.metrics["capital_required_usd"],
                cash_shortfall_usd=hold.metrics["cash_shortfall_usd"],
                model_net_return_pct=model.metrics.get("net_return_pct") if not model.lots.empty else None,
                hold_net_return_pct=hold.metrics.get("net_return_pct") if not model.lots.empty else None,
                model_pnl_usd=model.metrics.get("profit_usd") if not model.lots.empty else None,
                hold_pnl_usd=hold.metrics.get("profit_usd") if not model.lots.empty else None,
                hold_value_usd=hold.metrics.get("final_value_usd"),
                model_coverage_complete=bool(model.metrics.get("coverage_complete")),
                hold_coverage_complete=bool(hold.metrics.get("coverage_complete")),
                excess_return_pp=excess, outperformed_matched_hold=bool(excess > 0) if excess is not None else None,
                status=hold.metrics["reference_status"]))
    references = benchmark_bars if benchmark_bars is not None else bars
    benchmark_rows = []
    for mode, models in (("theoretical", theoretical), ("budget", budget)):
        for model_id, result in models.items():
            for name, ticker in BENCHMARKS.items():
                row = _benchmark(result, references.get(ticker, pd.DataFrame()), mode=mode,
                    start=start, end=end, budget_usd=budget_usd, costs=charges)
                raw_model_return = result.metrics.get("net_return_pct")
                model_return = float(raw_model_return) if raw_model_return is not None else None
                if model_return is not None and not math.isfinite(model_return):
                    model_return = None
                has_entries = not result.lots.empty
                model_complete = bool(result.metrics.get("coverage_complete"))
                benchmark_return = row.get("roi_pct")
                comparable = (has_entries and model_complete and bool(row.get("data_complete"))
                    and model_return is not None and benchmark_return is not None
                    and math.isfinite(float(benchmark_return)))
                excess = model_return - float(benchmark_return) if comparable else None
                row.update(model_net_return_pct=model_return if has_entries else None,
                    model_coverage_complete=model_complete,
                    excess_return_pp=excess,
                    outperformed_benchmark=bool(excess > 0) if excess is not None else None,
                    comparison_status="Comparación histórica valorable; no demuestra habilidad futura"
                    if comparable else "N/D: sin entradas o cobertura/valoración incompleta; no se afirma superación")
                benchmark_rows.append(dict(mode=mode, model=model_id, benchmark=ticker,
                    benchmark_name=name, source=source, **row))
    original_observations = (observed if cohort == "raw" else
                             normalize_audit_signals(snapshots, source=source, cohort="raw"))
    original = run_strategy_audit(original_observations.signals, bars, AuditConfig(start=start, end=end,
        amount_usd=100, entry_mode="daily", exit_policy="original"))
    legacy = _legacy_reference(bars, start, end)
    issue_tables = []
    if market_issues is not None and not market_issues.empty:
        issue_tables.append(market_issues.assign(mode="inputs", model="all"))
    for mode, models in (("theoretical", theoretical), ("budget", budget)):
        issue_tables.extend(result.issues.assign(mode=mode, model=model_id)
                            for model_id, result in models.items())
    issue_tables.extend(result.issues.assign(mode=mode + "_matched_hold", model=model_id)
                        for mode, models in matched_hold.items() for model_id, result in models.items())
    issues = pd.concat(issue_tables, ignore_index=True) if issue_tables else pd.DataFrame()
    root = Path(__file__).resolve().parent.parent
    workflow_hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in (
        "src/model_results_service.py", "src/model_results_ui.py", "scripts/run_model_results.py")}
    manifest = dict(service_version=SERVICE_VERSION, source=source,
        start=str(start), end=str(end), budget_usd=budget_usd, costs=asdict(charges),
        git_sha=git_sha, git_dirty=True if "dirty" in git_sha else None,
        workflow_source_hashes=workflow_hashes,
        observations=observed.manifest, coverage=observed.coverage,
        market=dict(market_manifest or {}), strategy_config=strategy,
        reports={mode: {name: result.manifest for name, result in models.items()}
                 for mode, models in (("theoretical", theoretical), ("budget", budget))},
        matched_hold={mode: {name: result.manifest for name, result in models.items()}
                      for mode, models in matched_hold.items()},
        matched_hold_rule="A/B/C each hold their own exact fills, quantities and entry times with identical external funding and entry costs. D remains A's hold strategy. Unfundable exact budget cohorts are N/D with explicit capital requirement; no forced financing.",
        original_reference=original.manifest,
        original_backtest_config=asdict(BacktestConfig()),
        original_reference_rule="Referencia separada: original usa lotes diarios externos100 y stops/trailing originales. Backtesting original conserva su asignador y configuración de capital/costes por activo en moneda nativa, sin conversión FX; nunca sumar esos capitales como USD ni compararlos automáticamente con A-D. No es la cartera A-D ni comparte sus aportaciones.",
        prospective_status="No activado: este informe es exclusivamente retrospectivo; no acredita correos ni operaciones.",
        limitations=["Datos reconstruidos; no cinta histórica de ejecuciones ni prueba de rentabilidad futura.",
            "Fuentes técnica y crecimiento se calculan por separado, sin fusionar notas.",
            "Benchmarks presupuestarios reciben una sola aportación inicial; los teóricos, las aportaciones ejecutadas de cada modelo.",
            "Una referencia incompleta sigue N/D. No se selecciona un ganador automáticamente."])
    if cohort != "raw":
        manifest.update(cohort=cohort, cohort_version=observed.manifest["cohort_version"],
            cohort_rule=observed.manifest["entry_selection"],
            original_reference_cohort="raw",
            original_reference_observations=original_observations.manifest)
        manifest["limitations"].extend(observed.manifest["cohort_limitations"])
    manifest["report_hash"] = _hash(manifest)
    return ModelResultsReport(observed, theoretical, budget, pd.DataFrame(benchmark_rows),
                              original, legacy, issues, manifest, matched_hold, pd.DataFrame(hold_rows))


def report_tables(report: ModelResultsReport) -> dict[str, pd.DataFrame]:
    summary, daily, lots, trades, attribution = [], [], [], [], []
    for mode, models in (("theoretical", report.theoretical), ("budget", report.budget)):
        for model_id, result in models.items():
            summary.append({**result.metrics, "mode": mode, "model": model_id})
            daily.append(result.daily.reset_index().assign(mode=mode, model=model_id))
            lots.append(result.lots.assign(mode=mode, model=model_id))
            trades.append(result.trades.assign(mode=mode, model=model_id))
            attribution.extend(dict(mode=mode, model=model_id, **row)
                               for row in result.metrics.get("attribution_by_ticker", []))
    return {"Resumen": pd.DataFrame(summary), "Evolución": pd.concat(daily, ignore_index=True),
        "Lotes": pd.concat(lots, ignore_index=True), "Ventas": pd.concat(trades, ignore_index=True),
        "Atribución": pd.DataFrame(attribution), "Índices": report.benchmarks,
        "Incidencias": report.issues, "Señales": report.signals,
        "Registros apartados": report.observations.rejected,
        "Referencia original": pd.DataFrame([report.original_reference.metrics]),
        "Lotes ref original": report.original_reference.lots,
        "Ventas ref original": report.original_reference.trades,
        "Backtest original separado": report.original_backtest,
        "Mantener mismos lotes": report.matched_hold_summary,
        "Lotes mantener": pd.concat([result.lots.assign(mode=mode, model=model_id)
            for mode, models in report.matched_hold.items() for model_id, result in models.items()], ignore_index=True),
        "Evolución mantener": pd.concat([result.daily.reset_index().assign(mode=mode, model=model_id)
            for mode, models in report.matched_hold.items() for model_id, result in models.items()], ignore_index=True)}


def export_model_results_excel(report: ModelResultsReport) -> bytes:
    """Safe multi-model workbook, entirely in memory; literal strings only.

    Uses the same xlsxwriter safety settings as strategy_audit_service.audit_excel
    with distinct sheets for this multi-model report and its attribution.
    """
    tables = report_tables(report)
    manifest = json.dumps(_safe(public_value(report.manifest)), ensure_ascii=False,
                          sort_keys=True, allow_nan=False)
    tables["Reglas y procedencia"] = pd.DataFrame([dict(part=index // 30000 + 1,
        content=manifest[index:index + 30000]) for index in range(0, len(manifest), 30000)])
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter", engine_kwargs={"options": {
        "strings_to_formulas": False, "strings_to_urls": False}}) as writer:
        for name, frame in tables.items():
            safe = public_frame(frame)
            for column in safe:
                safe[column] = safe[column].map(lambda value:
                    value.isoformat() if isinstance(value, (pd.Timestamp, datetime, date)) else
                    json.dumps(_safe(value), ensure_ascii=False, allow_nan=False)
                    if isinstance(value, (Mapping, list, tuple)) else value)
            safe.to_excel(writer, sheet_name=name[:31], index=False)
    return output.getvalue()
