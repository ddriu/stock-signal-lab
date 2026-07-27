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
