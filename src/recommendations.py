"""Guías probabilísticas de entrada y toma de beneficios."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ForwardReturnStudy:
    horizon_days: int
    samples: int
    median_return_pct: float | None
    mean_return_pct: float | None
    positive_rate_pct: float | None
    lower_quartile_pct: float | None
    upper_quartile_pct: float | None

    @property
    def reliable(self) -> bool:
        return self.samples >= 8


@dataclass(frozen=True)
class EntryGuide:
    label: str
    rationale: str
    initial_fraction: float
    initial_amount: float
    initial_quantity: float
    maximum_position_value: float
    maximum_quantity: float


@dataclass(frozen=True)
class ProfitLevel:
    name: str
    target_price: float
    quantity: float
    net_profit_vs_cost: float
    reached: bool


@dataclass(frozen=True)
class ProfitTakingPlan:
    levels: tuple[ProfitLevel, ...]
    suggested_sell_now_pct: float
    suggested_sell_now_quantity: float
    net_profit_if_sold_now: float
    trailing_quantity: float


def historical_forward_return_study(
    frame: pd.DataFrame,
    *,
    current_score: int,
    horizon_days: int = 20,
    score_tolerance: int = 10,
) -> ForwardReturnStudy:
    """Estudia retornos posteriores a estados históricos comparables sin solaparlos."""

    required = {"close", "signal_score", "sma_medium", "sma_long", "sma_medium_slope"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Faltan columnas para la estimación: {', '.join(sorted(missing))}")
    if horizon_days < 1:
        raise ValueError("El horizonte debe ser positivo.")

    data = frame.dropna(subset=list(required)).copy()
    comparable = (
        data["signal_score"].between(current_score - score_tolerance, current_score + score_tolerance)
        & (data["close"] > data["sma_medium"])
        & (data["close"] > data["sma_long"])
        & (data["sma_medium_slope"] > 0)
    )
    # Sólo tomamos el comienzo de cada estado y separamos observaciones un
    # horizonte completo para reducir el solapamiento y la falsa precisión.
    event = comparable & ~comparable.shift(1, fill_value=False)
    event_positions = np.flatnonzero(event.to_numpy())
    returns: list[float] = []
    next_allowed = 0
    closes = data["close"].to_numpy(dtype=float)
    for position in event_positions:
        if position < next_allowed or position + horizon_days >= len(data):
            continue
        returns.append((closes[position + horizon_days] / closes[position] - 1.0) * 100.0)
        next_allowed = position + horizon_days

    if not returns:
        return ForwardReturnStudy(horizon_days, 0, None, None, None, None, None)
    values = np.asarray(returns, dtype=float)
    return ForwardReturnStudy(
        horizon_days=horizon_days,
        samples=len(values),
        median_return_pct=float(np.median(values)),
        mean_return_pct=float(np.mean(values)),
        positive_rate_pct=float(np.mean(values > 0) * 100),
        lower_quartile_pct=float(np.percentile(values, 25)),
        upper_quartile_pct=float(np.percentile(values, 75)),
    )


def build_entry_guide(
    *,
    fundamental_score: int | None,
    technical_score: int,
    entry_label: str,
    maximum_position_value: float,
    current_price: float,
    study: ForwardReturnStudy,
) -> EntryGuide:
    """Propone entrada nula, parcial o escalonada dentro del límite de riesgo."""

    if maximum_position_value <= 0 or current_price <= 0:
        raise ValueError("Posición máxima y precio deben ser positivos.")

    historical_support = (
        study.reliable
        and study.median_return_pct is not None
        and study.positive_rate_pct is not None
        and study.median_return_pct > 0
        and study.positive_rate_pct >= 50
    )
    fraction = 0.0
    label = "Esperar"
    rationale = "El momento actual no justifica abrir una posición según las reglas configuradas."

    if entry_label == "Entrada fuerte" and fundamental_score is not None and fundamental_score >= 75:
        if historical_support:
            fraction = 0.50
            label = "Entrada escalonada"
            rationale = (
                "Empresa sólida, momento técnico fuerte y comportamiento histórico favorable. "
                "Se propone empezar con la mitad del tamaño máximo y reservar el resto."
            )
        else:
            fraction = 0.25
            label = "Entrada parcial prudente"
            rationale = (
                "Empresa y señal fuertes, pero la evidencia histórica comparable es insuficiente "
                "o no claramente favorable."
            )
    elif (
        entry_label in {"Entrada fuerte", "Entrada interesante"}
        and fundamental_score is not None
        and fundamental_score >= 60
    ):
        fraction = 0.25
        label = "Entrada parcial"
        rationale = (
            "La calidad y el momento permiten estudiar una entrada pequeña, manteniendo "
            "capital para una confirmación posterior."
        )
    elif entry_label in {"Entrada fuerte", "Entrada interesante"} and fundamental_score is None:
        fraction = 0.15
        label = "Entrada exploratoria"
        rationale = (
            "El momento técnico es atractivo, pero faltan fundamentales suficientes; "
            "por eso el tamaño propuesto es reducido."
        )
    elif technical_score >= 55:
        label = "Vigilar"
        rationale = "Hay señales prometedoras, pero todavía falta calidad o confirmación."

    initial_amount = maximum_position_value * fraction
    return EntryGuide(
        label=label,
        rationale=rationale,
        initial_fraction=fraction,
        initial_amount=initial_amount,
        initial_quantity=initial_amount / current_price,
        maximum_position_value=maximum_position_value,
        maximum_quantity=maximum_position_value / current_price,
    )


def build_profit_taking_plan(
    *,
    quantity: float,
    average_cost: float,
    current_price: float,
    stop_loss_pct: float,
    fee_per_sale: float = 1.0,
) -> ProfitTakingPlan:
    """Crea niveles 1R/2R/3R y conserva un 25% con protección dinámica."""

    if min(quantity, average_cost, current_price, stop_loss_pct) <= 0 or fee_per_sale < 0:
        raise ValueError("Cantidad, precios y stop deben ser positivos.")
    risk_per_share = average_cost * stop_loss_pct / 100.0
    level_quantity = quantity * 0.25
    levels: list[ProfitLevel] = []
    reached_count = 0
    for multiplier in (1, 2, 3):
        target = average_cost + multiplier * risk_per_share
        reached = current_price >= target
        reached_count += int(reached)
        levels.append(
            ProfitLevel(
                name=f"{multiplier}R",
                target_price=target,
                quantity=level_quantity,
                net_profit_vs_cost=level_quantity * (target - average_cost) - fee_per_sale,
                reached=reached,
            )
        )

    # Aunque se hayan superado los tres niveles, limita la venta inmediata al
    # 50% para no convertir una regla de toma de beneficios en una salida total.
    sell_now_pct = min(0.50, reached_count * 0.25)
    sell_now_quantity = quantity * sell_now_pct
    net_profit_now = (
        sell_now_quantity * (current_price - average_cost) - fee_per_sale
        if sell_now_quantity > 0
        else 0.0
    )
    return ProfitTakingPlan(
        levels=tuple(levels),
        suggested_sell_now_pct=sell_now_pct * 100,
        suggested_sell_now_quantity=sell_now_quantity,
        net_profit_if_sold_now=net_profit_now,
        trailing_quantity=quantity * (1 - sell_now_pct),
    )
