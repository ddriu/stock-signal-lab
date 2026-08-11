"""Métricas de cartera reutilizables para usuarios y administradores."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.data_sources import convert_currency
from src.data_loader import resolve_analysis_ticker
from src.journal import calculate_position_states
from src.portfolio import value_holding


@dataclass(frozen=True)
class PortfolioKpis:
    operations_count: int
    open_positions_count: int
    priced_positions_count: int
    invested_eur: float
    priced_cost_eur: float
    current_net_value_eur: float
    unrealized_pnl_eur: float
    unrealized_return_pct: float
    realized_pnl_eur: float
    fees_eur: float
    latest_activity: str | None


def _to_eur(
    value: float,
    currency: str,
    rates_per_eur: dict[str, float],
) -> float | None:
    try:
        return float(convert_currency(value, currency, "EUR", rates_per_eur))
    except ValueError:
        return None


def build_position_dashboard(
    operations: pd.DataFrame,
    positions: pd.DataFrame,
    latest_prices: dict[str, float],
    rates_per_eur: dict[str, float],
    *,
    sell_fee_eur: float = 1.0,
) -> tuple[pd.DataFrame, PortfolioKpis]:
    """Valora posiciones y calcula KPIs comparables en euros cuando es posible."""

    rows: list[dict[str, object]] = []
    invested_eur = 0.0
    priced_cost_eur = 0.0
    current_net_value_eur = 0.0
    unrealized_pnl_eur = 0.0
    priced_positions = 0

    for position in positions.itertuples(index=False):
        ticker = str(position.ticker)
        currency = str(position.currency).upper()
        cost_basis = float(position.cost_basis)
        cost_eur = _to_eur(cost_basis, currency, rates_per_eur)
        if cost_eur is not None:
            invested_eur += cost_eur

        analysis_ticker = resolve_analysis_ticker(ticker)
        current_price = latest_prices.get(analysis_ticker)
        net_value_eur: float | None = None
        net_pnl_eur: float | None = None
        net_return_pct: float | None = None
        if current_price is not None and float(current_price) > 0:
            try:
                sell_fee = convert_currency(
                    float(sell_fee_eur),
                    "EUR",
                    currency,
                    rates_per_eur,
                )
            except ValueError:
                sell_fee = float(sell_fee_eur)
            valuation = value_holding(
                quantity=float(position.quantity),
                average_cost=float(position.average_cost),
                cost_basis=cost_basis,
                current_price=float(current_price),
                sell_fee=float(sell_fee),
            )
            net_value_eur = _to_eur(
                float(valuation.net_exit_value),
                currency,
                rates_per_eur,
            )
            net_pnl_eur = _to_eur(
                float(valuation.net_pnl),
                currency,
                rates_per_eur,
            )
            if net_value_eur is not None and net_pnl_eur is not None and cost_eur is not None:
                priced_positions += 1
                priced_cost_eur += cost_eur
                current_net_value_eur += net_value_eur
                unrealized_pnl_eur += net_pnl_eur
                net_return_pct = float(valuation.net_return_pct)

        rows.append(
            {
                "ticker": ticker,
                "currency": currency,
                "quantity": float(position.quantity),
                "average_cost": float(position.average_cost),
                "cost_basis": cost_basis,
                "current_price": (
                    float(current_price) if current_price is not None else float("nan")
                ),
                "net_value_eur": (
                    net_value_eur if net_value_eur is not None else float("nan")
                ),
                "net_pnl_eur": (
                    net_pnl_eur if net_pnl_eur is not None else float("nan")
                ),
                "net_return_pct": (
                    net_return_pct if net_return_pct is not None else float("nan")
                ),
                "allocation_pct": float("nan"),
            }
        )

    dashboard = pd.DataFrame(rows)
    if not dashboard.empty and current_net_value_eur > 0:
        dashboard["allocation_pct"] = (
            dashboard["net_value_eur"] / current_net_value_eur * 100
        )

    realized_pnl_eur = 0.0
    fees_eur = 0.0
    states = calculate_position_states(operations, include_closed=True)
    for state in states.itertuples(index=False):
        currency = str(state.currency).upper()
        realized = _to_eur(float(state.realized_pnl), currency, rates_per_eur)
        fees = _to_eur(float(state.paid_fees), currency, rates_per_eur)
        if realized is not None:
            realized_pnl_eur += realized
        if fees is not None:
            fees_eur += fees

    latest_activity: str | None = None
    if not operations.empty and "executed_at" in operations:
        latest = pd.to_datetime(operations["executed_at"], errors="coerce").max()
        if pd.notna(latest):
            latest_activity = pd.Timestamp(latest).date().isoformat()

    unrealized_return_pct = (
        unrealized_pnl_eur / priced_cost_eur * 100 if priced_cost_eur > 0 else 0.0
    )
    kpis = PortfolioKpis(
        operations_count=len(operations),
        open_positions_count=len(positions),
        priced_positions_count=priced_positions,
        invested_eur=invested_eur,
        priced_cost_eur=priced_cost_eur,
        current_net_value_eur=current_net_value_eur,
        unrealized_pnl_eur=unrealized_pnl_eur,
        unrealized_return_pct=unrealized_return_pct,
        realized_pnl_eur=realized_pnl_eur,
        fees_eur=fees_eur,
        latest_activity=latest_activity,
    )
    return dashboard, kpis
