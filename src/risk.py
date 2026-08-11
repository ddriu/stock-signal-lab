"""Cálculos sencillos de tamaño de posición y riesgo monetario."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PositionPlan:
    """Plan matemático orientativo; no constituye una orden de inversión."""

    entry_price: float
    stop_price: float
    reference_target_2r: float
    risk_budget: float
    quantity: float
    position_value: float
    loss_at_stop: float


@dataclass(frozen=True)
class ProfitTarget:
    """Nivel de venta limitada expresado como múltiplo del riesgo inicial."""

    multiple_r: int
    price: float
    gross_profit: float
    net_profit_after_exit_fee: float


@dataclass(frozen=True)
class StopLadderLevel:
    """Stop que resultaría al alcanzar un nuevo máximo desde la compra."""

    peak_gain_pct: float
    peak_price: float
    stop_price: float
    locked_return_pct: float


@dataclass(frozen=True)
class ManualOrderPlan:
    """Plan para una orden concreta, tanto completa como fraccionaria."""

    capital: float
    entry_price: float
    quantity: float
    position_value: float
    capital_remaining: float
    stop_price: float
    risk_per_share: float
    market_loss_at_stop: float
    estimated_loss_with_fees: float
    risk_budget: float
    maximum_quantity_by_risk: float
    maximum_position_value_by_risk: float
    within_capital: bool
    within_risk_budget: bool
    fractional: bool
    targets: tuple[ProfitTarget, ...]
    stop_ladder: tuple[StopLadderLevel, ...]


def calculate_position_plan(
    *,
    capital: float,
    entry_price: float,
    stop_loss_pct: float,
    max_risk_pct: float,
) -> PositionPlan:
    """Dimensiona una posición para que el stop no supere el riesgo elegido.

    La cantidad queda limitada por el capital disponible y admite fracciones.
    El objetivo 2R es sólo una referencia riesgo/beneficio, no una predicción.
    """

    if capital <= 0 or entry_price <= 0:
        raise ValueError("Capital y precio de entrada deben ser positivos.")
    if not 0 < stop_loss_pct < 100 or not 0 < max_risk_pct <= 100:
        raise ValueError("Stop y riesgo deben estar entre 0 y 100%.")

    stop_price = entry_price * (1 - stop_loss_pct / 100)
    risk_per_share = entry_price - stop_price
    risk_budget = capital * max_risk_pct / 100
    quantity_by_risk = risk_budget / risk_per_share
    quantity_by_capital = capital / entry_price
    quantity = min(quantity_by_risk, quantity_by_capital)
    position_value = quantity * entry_price
    loss_at_stop = quantity * risk_per_share
    reference_target_2r = entry_price + 2 * risk_per_share
    return PositionPlan(
        entry_price=entry_price,
        stop_price=stop_price,
        reference_target_2r=reference_target_2r,
        risk_budget=risk_budget,
        quantity=quantity,
        position_value=position_value,
        loss_at_stop=loss_at_stop,
    )


def calculate_manual_order_plan(
    *,
    capital: float,
    entry_price: float,
    stop_loss_pct: float,
    max_risk_pct: float,
    quantity: float | None = None,
    investment_amount: float | None = None,
    trailing_stop_pct: float = 10.0,
    fee_per_order: float = 0.0,
) -> ManualOrderPlan:
    """Calcula una orden introducida por unidades o por importe.

    Los objetivos 1R/2R/3R y el trailing son reglas matemáticas, no previsiones.
    Las comisiones se muestran separadas del movimiento del precio para que una
    orden pequeña no parezca cumplir el presupuesto de riesgo cuando no lo hace.
    """

    if capital <= 0 or entry_price <= 0:
        raise ValueError("Capital y precio de compra deben ser positivos.")
    if not 0 < stop_loss_pct < 100 or not 0 < max_risk_pct <= 100:
        raise ValueError("Stop y riesgo deben estar entre 0 y 100%.")
    if not 0 <= trailing_stop_pct < 100 or fee_per_order < 0:
        raise ValueError("Trailing y comisión no pueden ser negativos.")
    supplied = int(quantity is not None) + int(investment_amount is not None)
    if supplied != 1:
        raise ValueError("Introduce unidades o importe, pero no ambos.")
    if quantity is not None:
        selected_quantity = float(quantity)
    else:
        amount = float(investment_amount or 0.0)
        if amount <= 0:
            raise ValueError("El importe de compra debe ser positivo.")
        selected_quantity = amount / entry_price
    if selected_quantity <= 0:
        raise ValueError("El número de acciones debe ser positivo.")

    position_value = selected_quantity * entry_price
    stop_price = entry_price * (1 - stop_loss_pct / 100)
    risk_per_share = entry_price - stop_price
    market_loss_at_stop = selected_quantity * risk_per_share
    estimated_loss_with_fees = market_loss_at_stop + 2 * fee_per_order
    risk_budget = capital * max_risk_pct / 100
    maximum_quantity_by_risk = max(0.0, (risk_budget - 2 * fee_per_order) / risk_per_share)
    maximum_quantity_by_capital = max(0.0, (capital - fee_per_order) / entry_price)
    maximum_quantity = min(maximum_quantity_by_risk, maximum_quantity_by_capital)

    targets = tuple(
        ProfitTarget(
            multiple_r=multiple,
            price=entry_price + multiple * risk_per_share,
            gross_profit=selected_quantity * multiple * risk_per_share,
            net_profit_after_exit_fee=(
                selected_quantity * multiple * risk_per_share - 2 * fee_per_order
            ),
        )
        for multiple in (1, 2, 3)
    )
    initial_stop = stop_price
    ladder: list[StopLadderLevel] = []
    for peak_gain_pct in (0.0, 5.0, 10.0, 15.0, 20.0, 25.0):
        peak_price = entry_price * (1 + peak_gain_pct / 100)
        trailing_candidate = (
            peak_price * (1 - trailing_stop_pct / 100)
            if trailing_stop_pct > 0
            else initial_stop
        )
        active_stop = max(initial_stop, trailing_candidate)
        ladder.append(
            StopLadderLevel(
                peak_gain_pct=peak_gain_pct,
                peak_price=peak_price,
                stop_price=active_stop,
                locked_return_pct=(active_stop / entry_price - 1) * 100,
            )
        )

    return ManualOrderPlan(
        capital=capital,
        entry_price=entry_price,
        quantity=selected_quantity,
        position_value=position_value,
        capital_remaining=capital - position_value - fee_per_order,
        stop_price=stop_price,
        risk_per_share=risk_per_share,
        market_loss_at_stop=market_loss_at_stop,
        estimated_loss_with_fees=estimated_loss_with_fees,
        risk_budget=risk_budget,
        maximum_quantity_by_risk=maximum_quantity,
        maximum_position_value_by_risk=maximum_quantity * entry_price,
        within_capital=(position_value + fee_per_order) <= capital + 1e-9,
        within_risk_budget=estimated_loss_with_fees <= risk_budget + 1e-9,
        fractional=abs(selected_quantity - round(selected_quantity)) > 1e-9,
        targets=targets,
        stop_ladder=tuple(ladder),
    )
