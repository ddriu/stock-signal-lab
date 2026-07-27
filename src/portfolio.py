"""Valoración de posiciones y comparación transparente de cambios."""

from __future__ import annotations

from dataclasses import dataclass

from src.data_sources import convert_currency


@dataclass(frozen=True)
class HoldingValuation:
    gross_value: float
    net_exit_value: float
    net_pnl: float
    net_return_pct: float
    break_even_price: float


@dataclass(frozen=True)
class SwitchComparison:
    compatible_currency: bool
    cash_after_sale: float
    cash_invested: float
    candidate_quantity: float
    immediate_fees: float
    fee_hurdle_pct: float
    conversion_rate: float | None = None
    fx_as_of: str | None = None


def value_holding(
    *,
    quantity: float,
    average_cost: float,
    cost_basis: float,
    current_price: float,
    sell_fee: float = 1.0,
) -> HoldingValuation:
    if min(quantity, average_cost, cost_basis, current_price) <= 0 or sell_fee < 0:
        raise ValueError("Cantidad, costes y precio deben ser positivos.")
    gross_value = quantity * current_price
    net_exit_value = max(0.0, gross_value - sell_fee)
    net_pnl = net_exit_value - cost_basis
    net_return_pct = net_pnl / cost_basis * 100
    break_even_price = (cost_basis + sell_fee) / quantity
    return HoldingValuation(
        gross_value=gross_value,
        net_exit_value=net_exit_value,
        net_pnl=net_pnl,
        net_return_pct=net_return_pct,
        break_even_price=break_even_price,
    )


def compare_switch(
    *,
    quantity: float,
    current_price: float,
    candidate_price: float,
    current_currency: str,
    candidate_currency: str,
    sell_fee: float = 1.0,
    buy_fee: float = 1.0,
    fx_rates_per_eur: dict[str, float] | None = None,
    fee_currency: str = "EUR",
    fx_as_of: str | None = None,
) -> SwitchComparison:
    if min(quantity, current_price, candidate_price) <= 0:
        raise ValueError("Cantidad y precios deben ser positivos.")
    if sell_fee < 0 or buy_fee < 0:
        raise ValueError("Las comisiones no pueden ser negativas.")
    current = current_currency.upper()
    candidate = candidate_currency.upper()
    same_currency = current == candidate
    can_convert = bool(
        fx_rates_per_eur
        and current in fx_rates_per_eur
        and candidate in fx_rates_per_eur
        and fee_currency.upper() in fx_rates_per_eur
    )
    compatible = same_currency or can_convert
    gross_value = quantity * current_price
    immediate_fees = sell_fee + buy_fee
    if fx_rates_per_eur and fee_currency.upper() in fx_rates_per_eur:
        try:
            sell_fee_current = convert_currency(
                sell_fee,
                fee_currency,
                current,
                fx_rates_per_eur,
            )
            buy_fee_candidate = convert_currency(
                buy_fee,
                fee_currency,
                candidate,
                fx_rates_per_eur,
            )
        except ValueError:
            sell_fee_current = sell_fee
            buy_fee_candidate = buy_fee
    else:
        sell_fee_current = sell_fee
        buy_fee_candidate = buy_fee
    cash_after_sale = max(0.0, gross_value - sell_fee_current)
    conversion_rate: float | None = None
    if compatible:
        if same_currency:
            conversion_rate = 1.0
            cash_in_candidate_currency = cash_after_sale
        else:
            conversion_rate = convert_currency(
                1.0,
                current,
                candidate,
                fx_rates_per_eur or {},
            )
            cash_in_candidate_currency = cash_after_sale * conversion_rate
        cash_invested = max(0.0, cash_in_candidate_currency - buy_fee_candidate)
        candidate_quantity = cash_invested / candidate_price
        buy_fee_current = (
            buy_fee_candidate
            if same_currency
            else buy_fee_candidate / conversion_rate
        )
        fee_hurdle_pct = (sell_fee_current + buy_fee_current) / gross_value * 100
    else:
        cash_invested = 0.0
        candidate_quantity = 0.0
        fee_hurdle_pct = immediate_fees / gross_value * 100
    return SwitchComparison(
        compatible_currency=compatible,
        cash_after_sale=cash_after_sale,
        cash_invested=cash_invested,
        candidate_quantity=candidate_quantity,
        immediate_fees=immediate_fees,
        fee_hurdle_pct=fee_hurdle_pct,
        conversion_rate=conversion_rate,
        fx_as_of=fx_as_of,
    )
