"""Frozen, causal model comparisons; simulations, never brokerage executions.

Signals and native-price stop decisions belong to ``strategy_audit_engine``.
This module only freezes four cohorts and adds an explicit USD cash ledger.
``Reducir`` includes the more severe ``Vender`` state: the original position
label gives sell_setup priority over reduce_setup. A/B/C use the evaluator's
close-based native hard stop, NOT the legacy backtest's intraday trailing-stop
variant. That distinct ``original`` engine policy remains unchanged. D
deliberately has no exits or stops.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import hashlib
from importlib.metadata import version, PackageNotFoundError
import math
import platform
from pathlib import Path
from types import MappingProxyType

import pandas as pd

from config import StrategyConfig
from src.strategy_audit_engine import (
    AuditConfig, AuditResult, ISSUE_COLUMNS, _boundary, _hash, _safe,
    run_strategy_audit,
)


MODEL_RESULTS_VERSION = "frozen-model-results-v1"


def runtime_versions() -> dict:
    result = {"python": platform.python_version()}
    for package in ("pandas", "numpy", "yfinance", "exchange-calendars"):
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = None
    return result


@dataclass(frozen=True)
class FrozenModelSpec:
    model_id: str
    name: str
    entry_mode: str
    exit_policy: str
    min_score: float | None = None
    amount_usd: float = 100.0


FROZEN_MODELS: Mapping[str, FrozenModelSpec] = MappingProxyType({
    "A": FrozenModelSpec("A", "Diaria · salir al reducir", "daily", "reduce"),
    "B": FrozenModelSpec("B", "Nueva activación · salir al vender", "activation", "sell"),
    "C": FrozenModelSpec("C", "Nueva activación ≥85 · salir al vender", "activation", "sell", 85),
    "D": FrozenModelSpec("D", "Mismas entradas de A · mantener", "daily", "hold"),
})


@dataclass(frozen=True)
class ExecutionCosts:
    """Per-side hypothetical charges; spread is adverse full-side bps.

    A buy retains exactly USD100 of raw-price exposure and pays charges on top.
    Fees do not change the native-price stop reference. FX bps are charged on
    each non-USD fill; missing currency is treated conservatively as foreign.
    ``verified`` describes supplied assumptions, never turns a run into real
    trading. Taxes, dividends, settlement delays and actual quotes are absent.
    """

    commission_fixed_usd: float = 1.0
    spread_bps: float = 10.0
    fx_bps: float = 25.0
    verified: bool = False

    def validate(self) -> None:
        for name in ("commission_fixed_usd", "spread_bps", "fx_bps"):
            value = _number(getattr(self, name))
            if value is None or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.spread_bps + self.fx_bps >= 10_000:
            raise ValueError("Combined spread/FX charges must be below 100% per side")

    def charge(self, gross_usd: float, currency: str | None = None) -> float:
        # Unknown currency is conservatively charged as foreign, never silently
        # assumed USD. Production inputs carry verified display_currency.
        fx_bps = 0.0 if currency == "USD" else self.fx_bps
        return self.commission_fixed_usd + gross_usd * (self.spread_bps + fx_bps) / 10_000


@dataclass(frozen=True)
class ModelResult:
    daily: pd.DataFrame
    lots: pd.DataFrame
    trades: pd.DataFrame
    issues: pd.DataFrame
    metrics: dict[str, object]
    manifest: dict[str, object]


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _issue(code: str, lot: Mapping | None = None, detail: str = "", severity: str = "info") -> dict:
    row = lot or {}
    return dict(code=code, ticker=row.get("ticker", ""), event_id=row.get("event_id", ""),
                session=row.get("entry_session", ""), detail=detail, severity=severity)


def _audit(signals: pd.DataFrame, bars: Mapping[str, pd.DataFrame], spec: FrozenModelSpec,
           start: object, end: object, *, selected_ids: set[str] | None = None) -> AuditResult:
    chosen = signals if selected_ids is None else signals.loc[signals.get(
        "event_id", pd.Series(index=signals.index, dtype=object)).astype(str).isin(selected_ids)].copy()
    config = AuditConfig(start=start, end=end,
        entry_mode=spec.entry_mode if selected_ids is None else "daily",
        min_score=spec.min_score if selected_ids is None else None,
        amount_usd=100.0, sizing="fixed",
        exit_policy=spec.exit_policy,
        strategy_config=StrategyConfig())
    return run_strategy_audit(chosen, bars, config)


def _budget_cohort(plan: AuditResult, budget: float, costs: ExecutionCosts, *, hold: bool
                   ) -> tuple[set[str], list[dict], list[str]]:
    """Choose whole USD100 lots, using only causally known sale cash.

    A fixed exit commission is reserved while a lot is open, ensuring no
    overdraft even when its price collapses. Intraday stop proceeds cannot fund
    earlier opens: their audit timestamp is conservatively the session close.
    Known exits at one timestamp precede buys at that same timestamp; this is
    an explicit instant-settlement simulation, not a brokerage claim.
    """
    events = []
    for lot in plan.lots.to_dict("records"):
        events.append((lot["entry_at"], 1, lot["ticker"], lot["event_id"], lot))
        if pd.notna(lot.get("exit_at")):
            events.append((lot["exit_at"], 0, lot["ticker"], lot["event_id"], lot))
    events.sort(key=lambda event: event[:4])
    cash, reserved = budget, 0.0
    selected: set[str] = set()
    issues: list[dict] = []
    rejected: list[str] = []
    reserve_each = 0.0 if hold else costs.commission_fixed_usd
    for _, phase, _, event_id, lot in events:
        if phase == 0:
            if event_id not in selected:
                continue
            gross = _number(lot.get("proceeds_usd"))
            # Unknown gross cannot be recycled; its worst possible net is the
            # reserved fixed commission, since proportional costs are <100%.
            cash += gross - costs.charge(gross, lot.get("currency")) if gross is not None else -costs.commission_fixed_usd
            reserved -= reserve_each
            if gross is None:
                issues.append(_issue("unknown_sale_cash_not_recycled", lot,
                    "Missing exit FX: retain only a conservative cash lower bound.", "warning"))
            continue
        debit = 100.0 + costs.charge(100.0, lot.get("currency"))
        if cash - reserved + 1e-9 < debit + reserve_each:
            rejected.append(event_id)
            issues.append(_issue("insufficient_budget", lot,
                "Whole USD100 entry rejected; no top-up, partial entry or overdraft."))
            continue
        selected.add(event_id)
        cash -= debit
        reserved += reserve_each
    return selected, issues, rejected


def _cost_lots(base: AuditResult, costs: ExecutionCosts, currencies: Mapping[str, str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    lots, trades = base.lots.copy(deep=True), base.trades.copy(deep=True)
    lots["currency"] = lots.ticker.map(currencies).fillna("")
    lots["entry_costs_usd"] = [costs.charge(float(value), currency) for value, currency in zip(lots.contribution_usd, lots.currency)]
    lots["entry_debit_usd"] = lots.contribution_usd + lots.entry_costs_usd
    lots["exit_costs_usd"] = [costs.charge(gross, currency) if (gross := _number(value)) is not None else
        (0.0 if status != "closed" else None) for value, status, currency in zip(lots.proceeds_usd, lots.status, lots.currency)]
    lots["net_proceeds_usd"] = [gross - fee if (gross := _number(value)) is not None and
        (fee := _number(fees)) is not None else None for value, fees in zip(lots.proceeds_usd, lots.exit_costs_usd)]
    lots["realized_pnl_usd"] = [net - debit if (net := _number(value)) is not None else None
        for value, debit in zip(lots.net_proceeds_usd, lots.entry_debit_usd)]
    lots["unrealized_pnl_usd"] = [value - debit if status != "closed" and
        (value := _number(mark)) is not None else (0.0 if status == "closed" else None)
        for mark, debit, status in zip(lots.value_usd, lots.entry_debit_usd, lots.status)]
    # Gross exposure and original native fill prices stay inspectable unchanged.
    lot_by_id = {row["lot_id"]: row for row in lots.to_dict("records")}
    for name in ("entry_costs_usd", "exit_costs_usd", "entry_debit_usd", "net_proceeds_usd"):
        trades[name] = [lot_by_id[identifier][name] for identifier in trades.lot_id]
    trades["gross_proceeds_usd"] = trades.proceeds_usd
    trades["gross_pnl_usd"] = trades.pnl_usd
    trades["pnl_usd"] = [lot_by_id[identifier]["realized_pnl_usd"] for identifier in trades.lot_id]
    trades["fees_usd"] = trades.entry_costs_usd + trades.exit_costs_usd
    trades["return_pct"] = trades.pnl_usd / trades.entry_debit_usd * 100.0
    return lots, trades


def _daily(base: AuditResult, lots: pd.DataFrame, mode: str, budget: float | None,
           costs: ExecutionCosts, *, hold: bool) -> pd.DataFrame:
    daily = base.daily.copy(deep=True)
    if daily.empty:
        for name in ("cash_usd", "cash_known_usd", "invested_capital_usd", "costs_usd",
                     "net_return_pct", "external_flow_usd", "entry_notional_usd", "reserved_cash_usd"):
            daily[name] = pd.Series(dtype=float)
        return daily
    previous_value, unit, peak, complete_path = 0.0, 1.0, 1.0, True
    records = lots.to_dict("records")
    accumulated_fees = 0.0
    for position, (day, source) in enumerate(daily.iterrows()):
        cutoff = pd.Timestamp(str(day) + "T23:59:59.999999999Z")
        existing = [lot for lot in records if lot["entry_at"] <= cutoff]
        closed = [lot for lot in existing if pd.notna(lot["exit_at"]) and lot["exit_at"] <= cutoff]
        active = [lot for lot in existing if pd.isna(lot["exit_at"]) or lot["exit_at"] > cutoff]
        deployed = math.fsum(lot["contribution_usd"] for lot in existing)
        entry_fees = math.fsum(lot["entry_costs_usd"] for lot in existing)
        exits_complete = all(_number(lot["net_proceeds_usd"]) is not None for lot in closed)
        known_net_sales = math.fsum(lot["net_proceeds_usd"] for lot in closed
                                  if _number(lot["net_proceeds_usd"]) is not None)
        unknown_sale_commissions = sum(_number(lot["net_proceeds_usd"]) is None for lot in closed) * costs.commission_fixed_usd
        total_fees = entry_fees + math.fsum(lot["exit_costs_usd"] for lot in closed
                                          if _number(lot["exit_costs_usd"]) is not None)
        external_total = deployed if mode == "theoretical" else float(budget)
        external_today = float(source.contribution_usd) if mode == "theoretical" else (float(budget) if position == 0 else 0.0)
        cash_known = external_total - deployed - entry_fees + known_net_sales - unknown_sale_commissions
        cash = cash_known if exits_complete else None
        open_value = _number(source.open_value_usd)
        complete = exits_complete and open_value is not None and bool(source.valuation_complete)
        value = cash + open_value if complete else None
        realized = math.fsum(lot["realized_pnl_usd"] for lot in closed) if exits_complete else None
        unrealized = open_value - math.fsum(lot["entry_debit_usd"] for lot in active) if open_value is not None else None
        reserved = 0.0 if hold else len(active) * costs.commission_fixed_usd
        fields = dict(entry_notional_usd=source.contribution_usd, external_flow_usd=external_today,
            contribution_usd=external_today, contributions_usd=external_total,
            withdrawal_usd=0.0, withdrawals_usd=0.0,
            gross_sale_proceeds_usd=source.withdrawals_usd,
            cash_usd=cash, cash_known_usd=cash_known, reserved_cash_usd=reserved,
            available_cash_usd=cash - reserved if cash is not None else None,
            invested_capital_usd=math.fsum(lot["contribution_usd"] for lot in active),
            costs_usd=total_fees - accumulated_fees, cumulative_costs_usd=total_fees,
            value_usd=value, value_known_usd=cash_known + source.open_value_known_usd,
            realized_pnl_usd=realized, unrealized_pnl_usd=unrealized,
            valuation_complete=complete, net_return_pct=(value / external_total - 1) * 100
            if value is not None and external_total > 0 else None,
            unit_value_daily_approx=None, drawdown_daily_approx_pct=None)
        accumulated_fees = total_fees
        if not complete:
            complete_path = False
        denominator = previous_value + external_today
        if complete_path and denominator > 0:
            unit *= value / denominator
            peak = max(peak, unit)
            fields["unit_value_daily_approx"] = unit
            fields["drawdown_daily_approx_pct"] = (unit / peak - 1) * 100
        previous_value = value if value is not None else 0.0
        for name, value in fields.items():
            daily.loc[day, name] = value
    return daily


def _xirr(lots: pd.DataFrame, final_value: float | None, start: object, end: object,
          mode: str, budget: float | None, complete: bool) -> tuple[float | None, str]:
    if lots.empty:
        return None, "Sin inversiones ejecutadas."
    if not complete or final_value is None:
        return None, "Valoraciones o flujos incompletos; no se calcula TIR."
    finish = _boundary(end, True)
    if mode == "budget":
        begin = _boundary(start, False) or lots.entry_at.min()
        flows = [(begin, -float(budget))]
    else:
        flows = [(row.entry_at, -float(row.entry_debit_usd)) for row in lots.itertuples()]
    begin = min(stamp for stamp, _ in flows)
    if finish is None or (finish - begin).total_seconds() < 365.25 * 86400:
        return None, "Horizonte inferior a un año; no se presenta una anualización de corto plazo."
    if final_value <= 0:
        return None, "No existe flujo terminal positivo para una TIR finita."
    flows.append((finish, final_value))
    times = [(stamp - begin).total_seconds() / (365.25 * 86400) for stamp, _ in flows]

    def npv(log_rate: float) -> float:
        exponents = [-years * log_rate for years in times]
        shift = max(exponents)
        return math.fsum(amount * math.exp(power - shift)
                         for (_, amount), power in zip(flows, exponents))

    low, high = -25.0, 25.0
    if npv(low) * npv(high) > 0:
        return None, "No se encuentra una raíz numéricamente estable."
    for _ in range(160):
        midpoint = (low + high) / 2
        if npv(midpoint) > 0:
            low = midpoint
        else:
            high = midpoint
    return math.expm1((low + high) / 2) * 100, "TIR anual sobre flujos externos fechados y patrimonio final; efectivo de ventas retenido."


def _max_capital(lots: pd.DataFrame) -> float:
    events = []
    for row in lots.itertuples():
        events.append((row.entry_at, 1, float(row.contribution_usd)))
        if pd.notna(row.exit_at):
            events.append((row.exit_at, 0, -float(row.contribution_usd)))
    current = peak = 0.0
    for _, _, amount in sorted(events):
        current += amount
        peak = max(peak, current)
    return peak


def _metrics(base: AuditResult, daily: pd.DataFrame, lots: pd.DataFrame, trades: pd.DataFrame,
             mode: str, budget: float | None, costs: ExecutionCosts, start: object, end: object) -> dict:
    final = daily.iloc[-1] if not daily.empty else None
    value = _number(final.value_usd) if final is not None else (budget if mode == "budget" else 0.0)
    external = float(budget) if mode == "budget" else float(lots.contribution_usd.sum())
    closed = lots.loc[lots.status.eq("closed")]
    opened = lots.loc[~lots.status.eq("closed")]
    realized_complete = closed.realized_pnl_usd.notna().all()
    unrealized_complete = opened.unrealized_pnl_usd.notna().all()
    realized = float(closed.realized_pnl_usd.sum()) if realized_complete else None
    unrealized = float(opened.unrealized_pnl_usd.sum()) if unrealized_complete else None
    winners = closed.loc[closed.realized_pnl_usd.gt(0), "realized_pnl_usd"]
    losers = closed.loc[closed.realized_pnl_usd.lt(0), "realized_pnl_usd"]
    gross_profit = float(winners.sum()) if realized_complete else None
    gross_loss = abs(float(losers.sum())) if realized_complete else None
    daily_complete = not daily.empty and bool(daily.valuation_complete.all())
    unit = _number(final.unit_value_daily_approx) if final is not None else None
    xirr, xirr_reason = _xirr(lots, value, start, end, mode, budget, daily_complete)
    attribution = []
    for ticker, group in lots.groupby("ticker", sort=True):
        closed_group = group.loc[group.status.eq("closed")]
        open_group = group.loc[~group.status.eq("closed")]
        rp = float(closed_group.realized_pnl_usd.sum()) if closed_group.realized_pnl_usd.notna().all() else None
        up = float(open_group.unrealized_pnl_usd.sum()) if open_group.unrealized_pnl_usd.notna().all() else None
        known_fees = float(group.entry_costs_usd.sum() + group.exit_costs_usd.sum())
        attribution.append(dict(ticker=ticker, n_lots=len(group), invested_usd=float(group.contribution_usd.sum()),
            realized_pnl_usd=rp, unrealized_pnl_usd=up,
            profit_usd=rp + up if rp is not None and up is not None else None,
            costs_usd=known_fees if group.exit_costs_usd.notna().all() else None,
            costs_known_usd=known_fees))
    gross_sales = float(trades.proceeds_usd.sum()) if trades.proceeds_usd.notna().all() else None
    gross_buys = float(lots.contribution_usd.sum())
    mean_wealth = float(daily.value_usd.mean()) if daily_complete else None
    turnover = (gross_buys + gross_sales) / mean_wealth * 100 if gross_sales is not None and mean_wealth and mean_wealth > 0 else None
    metrics = dict(base.metrics)
    metrics.update(contributions_usd=external, external_contributions_usd=external,
        invested_notional_usd=gross_buys, withdrawals_usd=0.0,
        final_value_usd=value, value_usd=value,
        value_known_usd=_number(final.value_known_usd) if final is not None else external,
        open_value_usd=_number(final.open_value_usd) if final is not None else 0.0,
        open_value_known_usd=_number(final.open_value_known_usd) if final is not None else 0.0,
        withdrawals_known_usd=0.0,
        cash_usd=_number(final.cash_usd) if final is not None else external,
        cash_known_usd=_number(final.cash_known_usd) if final is not None else external,
        available_cash_usd=_number(final.available_cash_usd) if final is not None else external,
        reserved_cash_usd=_number(final.reserved_cash_usd) if final is not None else 0.0,
        realized_pnl_usd=realized, unrealized_pnl_usd=unrealized,
        realized_pnl_known_usd=float(closed.realized_pnl_usd.sum()),
        unrealized_pnl_known_usd=float(opened.unrealized_pnl_usd.sum()),
        profit_usd=value - external if value is not None else None,
        roi_pct=(value / external - 1) * 100 if value is not None and external > 0 else None,
        net_return_pct=(value / external - 1) * 100 if value is not None and external > 0 else None,
        twr_pct=None, twr_daily_approx_pct=(unit - 1) * 100 if unit is not None and not lots.empty else None,
        twr_method="Aproximación diaria; flujos externos al inicio del día, sin puentear valoraciones ausentes.",
        twr_unavailable_reason="Faltan valoraciones diarias." if not daily_complete else None,
        max_drawdown_pct=None,
        max_drawdown_daily_approx_pct=float(daily.drawdown_daily_approx_pct.min())
        if daily_complete and daily.drawdown_daily_approx_pct.notna().any() else None,
        xirr_pct=xirr, xirr_reason=xirr_reason,
        win_rate_pct=float((closed.realized_pnl_usd > 0).mean() * 100) if len(closed) and realized_complete else None,
        gross_profit_usd=gross_profit, gross_loss_usd=gross_loss,
        profit_factor=gross_profit / gross_loss if gross_loss and gross_profit is not None else None,
        gain_loss_ratio=float(winners.mean() / abs(losers.mean())) if len(winners) and len(losers) and realized_complete else None,
        gain_loss_ratio_reason=None if len(winners) and len(losers) and realized_complete else "Requiere ganancias y pérdidas cerradas conocidas.",
        turnover_pct=turnover, turnover_definition="(Compras brutas + ventas brutas) / patrimonio medio diario; no anualizado.",
        average_duration_days=float(trades.duration_days.mean()) if not trades.empty else None,
        average_open_duration_days=float(opened.duration_days.mean()) if not opened.empty else None,
        max_simultaneous_invested_capital_usd=_max_capital(lots),
        fees_usd=float(lots.entry_costs_usd.sum() + lots.exit_costs_usd.sum()) if lots.exit_costs_usd.notna().all() else None,
        fees_known_usd=float(lots.entry_costs_usd.sum() + lots.exit_costs_usd.sum()), tax_usd=0.0,
        attribution_by_ticker=attribution,
        valuation_complete=bool(final.valuation_complete) if final is not None else True,
        coverage_complete=bool(base.metrics["coverage_complete"] and daily_complete),
        mode=mode, is_real_execution=False, cost_assumptions_verified=costs.verified)
    complete_cuts = daily.loc[daily.valuation_complete & daily.contributions_usd.gt(0)] if not daily.empty else daily
    last_cut = complete_cuts.iloc[-1] if not complete_cuts.empty else None
    metrics.update(last_complete_valuation_date=str(complete_cuts.index[-1]) if last_cut is not None else None,
        last_complete_value_usd=_number(last_cut.value_usd) if last_cut is not None else None,
        last_complete_contributions_usd=_number(last_cut.contributions_usd) if last_cut is not None else None,
        last_complete_roi_pct=_number(last_cut.net_return_pct) if last_cut is not None else None)
    return metrics


def run_frozen_models(signals: pd.DataFrame, bars: Mapping[str, pd.DataFrame], *,
                      start: object, end: object, mode: str = "theoretical",
                      budget_usd: float | None = None, costs: ExecutionCosts | None = None,
                      git_sha: str = "unknown") -> Mapping[str, ModelResult]:
    """Run exactly A/B/C/D without changing any indicator or StrategyConfig.

    Theoretical runs have unlimited independent USD100 external contributions,
    zero costs, and segregated sale cash. Budget runs receive one fixed initial
    funding, can reuse known net sale cash, and reject unaffordable whole lots.
    D requests exactly A's executed cohort, but never hides a capital rejection:
    ``cohort_matches_A`` and explicit missing/common IDs govern comparability.
    """
    if mode not in {"theoretical", "budget"}:
        raise ValueError("mode must be theoretical or budget")
    begin, finish = _boundary(start, False), _boundary(end, True)
    if begin is None or finish is None or begin > finish:
        raise ValueError("An ordered, explicit start/end range is required")
    charges = costs or (ExecutionCosts(0, 0, 0) if mode == "theoretical" else ExecutionCosts())
    charges.validate()
    if mode == "theoretical":
        if budget_usd is not None:
            raise ValueError("Theoretical mode has no fixed budget")
        if any((charges.commission_fixed_usd, charges.spread_bps, charges.fx_bps)):
            raise ValueError("Theoretical mode is explicitly zero-cost; use budget mode for charges")
    else:
        budget_usd = 6000.0 if budget_usd is None else budget_usd
        if _number(budget_usd) is None or float(budget_usd) <= 0:
            raise ValueError("budget mode requires a finite positive budget_usd")
    currencies = {str(ticker).strip().upper(): str(frame.attrs.get("display_currency") or
        frame.attrs.get("currency") or "").upper() for ticker, frame in bars.items()}
    source_root = Path(__file__).resolve().parent.parent
    source_hashes = {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest() for name in (
        "config.py", "src/indicators.py", "src/signal_engine.py", "src/backtesting.py",
        "src/strategy_audit_engine.py", "src/strategy_audit_data.py", "src/strategy_audit_service.py",
        "src/model_results_engine.py", "src/price_units.py", "src/instruments.py", "src/analysis_policy.py")}
    input_hash = _hash({"signals": signals, "bars": {str(ticker): {"frame": frame, "attrs": frame.attrs}
                                                     for ticker, frame in sorted(bars.items())}})
    results: dict[str, ModelResult] = {}
    a_ids: set[str] = set()
    for model_id, spec in FROZEN_MODELS.items():
        # D is anchored to A's actually executed entries within this modality.
        plan = _audit(signals, bars, spec, start, end,
                      selected_ids=a_ids if model_id == "D" else None)
        plan.lots["currency"] = plan.lots.ticker.map(currencies).fillna("")
        planned_ids = set(plan.lots.event_id.astype(str))
        if mode == "budget":
            selected, extra_issues, rejected = _budget_cohort(plan, float(budget_usd), charges,
                                                           hold=spec.exit_policy == "hold")
        else:
            selected, extra_issues, rejected = planned_ids, [], []
        base = plan if selected == planned_ids else _audit(signals, bars, spec, start, end, selected_ids=selected)
        if model_id == "A":
            a_ids = selected
        lots, trades = _cost_lots(base, charges, currencies)
        if mode == "budget" and charges.fx_bps > 0:
            for ticker in sorted(set(lots.loc[lots.currency.eq(""), "ticker"])):
                extra_issues.append(_issue("fx_cost_currency_unverified", {"ticker": ticker},
                    "Missing currency: conservatively charge configured FX bps; this is an unverified cost assumption.", "warning"))
        daily = _daily(base, lots, mode, budget_usd, charges, hold=spec.exit_policy == "hold")
        metrics = _metrics(base, daily, lots, trades, mode, budget_usd, charges, start, end)
        missing_a = sorted(a_ids - selected) if model_id == "D" else []
        if missing_a:
            extra_issues.append(_issue("hold_cohort_incomplete", detail=
                "D cannot fund all A entries without sale recycling; totals are not a matched-cohort counterfactual.", severity="warning"))
        all_issues = pd.concat([plan.issues, pd.DataFrame(extra_issues, columns=ISSUE_COLUMNS)], ignore_index=True).drop_duplicates(ignore_index=True)
        metrics.update(planned_entries=len(planned_ids), budget_rejected_entries=len(rejected),
                       executed_entries=len(selected), cohort_matches_A=not missing_a if model_id == "D" else None)
        # Preserve failed planned entries and input problems after budget filtering.
        metrics["coverage_complete"] = bool(metrics["coverage_complete"] and plan.metrics["coverage_complete"]
            and not missing_a and not any(row["code"] == "fx_cost_currency_unverified" for row in extra_issues))
        metrics["eligible_entries"] = plan.metrics["eligible_entries"]
        metrics["unfilled_entries"] = plan.metrics["unfilled_entries"] + len(rejected)
        manifest = dict(base.manifest)
        manifest.update(engine_version=MODEL_RESULTS_VERSION, audit_engine_version=base.manifest["engine_version"],
            runtime_versions=runtime_versions(),
            git_sha=git_sha, model=_safe(asdict(spec)), mode=mode,
            strategy_config=_safe(asdict(StrategyConfig())), input_hash=input_hash,
            strategy_config_hash=_hash(asdict(StrategyConfig())), source_hashes=source_hashes,
            model_run_hash=_hash({"version": MODEL_RESULTS_VERSION, "input": input_hash,
                "spec": asdict(spec), "start": begin, "end": finish, "mode": mode,
                "budget_usd": budget_usd, "costs": asdict(charges), "git_sha": git_sha,
                "source_hashes": source_hashes}),
            start=begin.isoformat(), end=finish.isoformat(), budget_usd=budget_usd,
            costs=_safe(asdict(charges)), is_real_execution=False,
            intended_event_ids=sorted(planned_ids), filled_event_ids=sorted(selected),
            budget_rejected_event_ids=sorted(rejected),
            requested_A_event_ids=sorted(a_ids) if model_id == "D" else None,
            cohort_matches_A=not missing_a if model_id == "D" else None,
            missing_A_event_ids=missing_a,
            compatible_A_D_event_ids=sorted(a_ids & selected) if model_id == "D" else None,
            reduce_interpretation="Reducir incluye Vender (sell_setup tiene precedencia); no se esperan cruces exclusivos de etiqueta.",
            stops_rule="A/B/C conservan stop nativo al cierre -8% del evaluador posición; ejecutan apertura siguiente con gaps. No se añade trailing intradía. D hold no tiene salidas. La referencia original/backtesting queda separada e intacta.",
            wealth_rule="Theoretical: cada entrada recibe USD100 externos; ventas retenidas sin reutilizar. Budget: una aportación fija; ventas netas conocidas reutilizables, sin recargas ni sobregiro.",
            cost_rule="Entrada: notional100 + comisión fija + spread bps y FX bps sólo noUSD (o moneda no verificada, supuesto conservador). Salida: bruto menos cargos; costes no cambian stops. Reserva de comisión de salida en lotes no hold.",
            settlement_rule="Efectivo de ventas conocido disponible en timestamp auditado; salidas antes de entradas simultáneas. Stops intradía sólo liberan efectivo al cierre proxy.",
            coverage_complete=metrics["coverage_complete"],
            limitations=["Simulación, no operaciones reales; sin impuestos, dividendos ni retraso de liquidación.",
                "Los costes son hipótesis configurables, no cotizaciones de broker.",
                "TWR/drawdown son aproximaciones diarias con flujos al inicio del día; no se puentean datos faltantes.",
                "XIRR no se publica antes de un año ni con valoraciones incompletas.",
                "Falta de historial previo puede censurar activaciones: no se inventa un estado anterior.",
                "D sólo es comparable por cohorte con A cuando cohort_matches_A es true."])
        results[model_id] = ModelResult(daily, lots, trades, all_issues, metrics, manifest)
    return MappingProxyType(results)
