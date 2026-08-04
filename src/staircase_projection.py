"""Proyecciones explicables para el plan mensual y la estrategia escalonada.

El módulo separa capital aportado y rentabilidad. No intenta predecir el mercado:
calcula escenarios deterministas y una distribución Monte Carlo a partir de
supuestos editables. La aportación a la estrategia dinámica sólo aumenta cuando
su rendimiento anual observado supera el umbral configurado.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class StaircaseProjectionConfig:
    """Capital, aportaciones y reglas de ampliación del bloque dinámico."""

    start_date: date
    initial_civislend: float = 1_500.0
    initial_factoring: float = 1_850.0
    initial_equities: float = 4_400.0
    initial_staircase: float = 640.0
    monthly_total: float = 1_000.0
    monthly_civislend: float = 250.0
    monthly_factoring: float = 250.0
    initial_staircase_pct: float = 20.0
    staircase_step_pct: float = 5.0
    maximum_staircase_pct: float = 40.0
    scale_return_threshold_pct: float = 10.0
    civislend_return_pct: float = 10.5
    factoring_return_pct: float = 6.0
    traditional_equity_return_pct: float = 8.0
    traditional_equity_volatility_pct: float = 16.0
    staircase_volatility_pct: float = 28.0

    def validate(self) -> None:
        amounts = (
            self.initial_civislend,
            self.initial_factoring,
            self.initial_equities,
            self.initial_staircase,
            self.monthly_total,
            self.monthly_civislend,
            self.monthly_factoring,
        )
        if any(value < 0 for value in amounts):
            raise ValueError("Capitales y aportaciones no pueden ser negativos.")
        if self.initial_staircase > self.initial_equities:
            raise ValueError(
                "El capital inicial escalonado no puede superar las acciones totales."
            )
        fixed_monthly = self.monthly_civislend + self.monthly_factoring
        if fixed_monthly > self.monthly_total:
            raise ValueError(
                "Civislend y facturas no pueden superar la aportación mensual total."
            )
        percentages = (
            self.initial_staircase_pct,
            self.staircase_step_pct,
            self.maximum_staircase_pct,
        )
        if any(value < 0 or value > 100 for value in percentages):
            raise ValueError("Los porcentajes de la escalera deben estar entre 0 y 100.")
        if self.initial_staircase_pct > self.maximum_staircase_pct:
            raise ValueError("El porcentaje inicial no puede superar el máximo dinámico.")
        available_equity_pct = (
            100.0
            if self.monthly_total == 0
            else 100.0 * (self.monthly_total - fixed_monthly) / self.monthly_total
        )
        if self.maximum_staircase_pct > available_equity_pct + 1e-9:
            raise ValueError(
                "El máximo dinámico deja una aportación tradicional negativa."
            )
        returns_and_volatility = (
            self.civislend_return_pct,
            self.factoring_return_pct,
            self.traditional_equity_return_pct,
            self.traditional_equity_volatility_pct,
            self.staircase_volatility_pct,
        )
        if any(value <= -100 for value in returns_and_volatility[:3]):
            raise ValueError("Las rentabilidades deben ser superiores a -100%.")
        if any(value < 0 for value in returns_and_volatility[3:]):
            raise ValueError("La volatilidad no puede ser negativa.")

    @property
    def initial_total(self) -> float:
        return self.initial_civislend + self.initial_factoring + self.initial_equities

    @property
    def initial_traditional_equities(self) -> float:
        return self.initial_equities - self.initial_staircase


@dataclass(frozen=True)
class ProjectionScenario:
    """Rentabilidad anual neta supuesta para la estrategia escalonada."""

    label: str
    staircase_return_pct: float


DEFAULT_SCENARIOS: tuple[ProjectionScenario, ...] = (
    ProjectionScenario("Prudente 6%", 6.0),
    ProjectionScenario("Central 10%", 10.0),
    ProjectionScenario("Ambicioso 15%", 15.0),
    ProjectionScenario("Excepcional 20%", 20.0),
)


def months_until_year_end(start: date) -> int:
    """Cuenta aportaciones mensuales desde el mes actual hasta diciembre."""

    return 12 - int(start.month) + 1


def default_horizons(start: date) -> tuple[tuple[str, int], ...]:
    return (
        (f"Diciembre {start.year}", months_until_year_end(start)),
        ("12 meses", 12),
        ("24 meses", 24),
        ("36 meses", 36),
        ("48 meses", 48),
        ("10 años", 120),
    )


def _monthly_rate(annual_return_pct: float) -> float:
    if annual_return_pct <= -100:
        raise ValueError("La rentabilidad anual debe ser superior a -100%.")
    return (1.0 + annual_return_pct / 100.0) ** (1.0 / 12.0) - 1.0


def staircase_allocation_pct(
    config: StaircaseProjectionConfig,
    *,
    completed_years: int,
    staircase_return_pct: float,
) -> float:
    """Amplía la aportación sólo si el escenario supera el umbral anual."""

    if staircase_return_pct < config.scale_return_threshold_pct:
        return config.initial_staircase_pct
    return min(
        config.initial_staircase_pct
        + max(int(completed_years), 0) * config.staircase_step_pct,
        config.maximum_staircase_pct,
    )


def project_scenario(
    config: StaircaseProjectionConfig,
    scenario: ProjectionScenario,
    *,
    months: int = 120,
) -> pd.DataFrame:
    """Proyecta mensualmente cada bloque y conserva el capital aportado."""

    config.validate()
    if months <= 0:
        raise ValueError("El horizonte debe contener al menos un mes.")

    civislend = float(config.initial_civislend)
    factoring = float(config.initial_factoring)
    traditional = float(config.initial_traditional_equities)
    staircase = float(config.initial_staircase)
    contributed = float(config.initial_total)
    rates = {
        "civislend": _monthly_rate(config.civislend_return_pct),
        "factoring": _monthly_rate(config.factoring_return_pct),
        "traditional": _monthly_rate(config.traditional_equity_return_pct),
        "staircase": _monthly_rate(scenario.staircase_return_pct),
    }
    rows: list[dict[str, object]] = []
    start_timestamp = pd.Timestamp(config.start_date)

    for month in range(1, int(months) + 1):
        completed_years = (month - 1) // 12
        dynamic_pct = staircase_allocation_pct(
            config,
            completed_years=completed_years,
            staircase_return_pct=scenario.staircase_return_pct,
        )
        dynamic_contribution = config.monthly_total * dynamic_pct / 100.0
        traditional_contribution = max(
            config.monthly_total
            - config.monthly_civislend
            - config.monthly_factoring
            - dynamic_contribution,
            0.0,
        )

        civislend = civislend * (1 + rates["civislend"]) + config.monthly_civislend
        factoring = factoring * (1 + rates["factoring"]) + config.monthly_factoring
        traditional = traditional * (1 + rates["traditional"]) + traditional_contribution
        staircase = staircase * (1 + rates["staircase"]) + dynamic_contribution
        contributed += config.monthly_total
        total_value = civislend + factoring + traditional + staircase
        rows.append(
            {
                "scenario": scenario.label,
                "month": month,
                "date": start_timestamp + pd.offsets.MonthEnd(month),
                "contributed": contributed,
                "civislend": civislend,
                "factoring": factoring,
                "traditional_equities": traditional,
                "staircase": staircase,
                "staircase_allocation_pct": dynamic_pct,
                "total_value": total_value,
                "estimated_profit": total_value - contributed,
            }
        )
    return pd.DataFrame(rows)


def project_scenarios(
    config: StaircaseProjectionConfig,
    scenarios: Iterable[ProjectionScenario] = DEFAULT_SCENARIOS,
    *,
    months: int = 120,
) -> pd.DataFrame:
    frames = [project_scenario(config, scenario, months=months) for scenario in scenarios]
    if not frames:
        raise ValueError("Añade al menos un escenario.")
    return pd.concat(frames, ignore_index=True)


def summarize_projection(
    projections: pd.DataFrame,
    horizons: Iterable[tuple[str, int]],
) -> pd.DataFrame:
    """Crea una tabla ancha para diciembre y los horizontes solicitados."""

    if projections.empty:
        return pd.DataFrame()
    scenario_order = list(dict.fromkeys(projections["scenario"].astype(str)))
    rows: list[dict[str, object]] = []
    for label, month in horizons:
        horizon_rows = projections.loc[projections["month"] == int(month)]
        if horizon_rows.empty:
            continue
        row: dict[str, object] = {
            "Horizonte": label,
            "Meses": int(month),
            "Total aportado": float(horizon_rows["contributed"].iloc[0]),
        }
        for scenario in scenario_order:
            value = horizon_rows.loc[
                horizon_rows["scenario"] == scenario, "total_value"
            ]
            row[scenario] = float(value.iloc[0]) if not value.empty else float("nan")
        rows.append(row)
    return pd.DataFrame(rows)


def simulate_projection_ranges(
    config: StaircaseProjectionConfig,
    *,
    expected_staircase_return_pct: float = 10.0,
    simulations: int = 1_000,
    months: int = 120,
    seed: int = 42,
) -> pd.DataFrame:
    """Simula percentiles con ampliación anual basada en el resultado observado.

    Los retornos de acciones tradicionales y de la escalera se modelan como
    lognormales mensuales correlacionadas de forma moderada. Civislend y factoring
    utilizan las tasas deterministas configuradas porque su valoración no fluctúa
    diariamente como una acción cotizada.
    """

    config.validate()
    if simulations < 100:
        raise ValueError("Usa al menos 100 simulaciones.")
    if months <= 0:
        raise ValueError("El horizonte debe contener al menos un mes.")

    rng = np.random.default_rng(seed)
    n = int(simulations)
    traditional = np.full(n, config.initial_traditional_equities, dtype=float)
    staircase = np.full(n, config.initial_staircase, dtype=float)
    civislend = np.full(n, config.initial_civislend, dtype=float)
    factoring = np.full(n, config.initial_factoring, dtype=float)
    dynamic_pct = np.full(n, config.initial_staircase_pct, dtype=float)
    rolling_growth = np.ones(n, dtype=float)
    contributed = float(config.initial_total)

    traditional_mean = np.log1p(config.traditional_equity_return_pct / 100.0) / 12.0
    staircase_mean = np.log1p(expected_staircase_return_pct / 100.0) / 12.0
    traditional_sigma = config.traditional_equity_volatility_pct / 100.0 / np.sqrt(12)
    staircase_sigma = config.staircase_volatility_pct / 100.0 / np.sqrt(12)
    civislend_rate = _monthly_rate(config.civislend_return_pct)
    factoring_rate = _monthly_rate(config.factoring_return_pct)
    start_timestamp = pd.Timestamp(config.start_date)
    rows: list[dict[str, object]] = []

    for month in range(1, int(months) + 1):
        common = rng.standard_normal(n)
        independent = rng.standard_normal(n)
        traditional_log_return = (
            traditional_mean - 0.5 * traditional_sigma**2 + traditional_sigma * common
        )
        staircase_shock = 0.45 * common + np.sqrt(1 - 0.45**2) * independent
        staircase_log_return = (
            staircase_mean - 0.5 * staircase_sigma**2 + staircase_sigma * staircase_shock
        )
        traditional_return = np.exp(traditional_log_return)
        staircase_return = np.exp(staircase_log_return)
        rolling_growth *= staircase_return

        dynamic_contribution = config.monthly_total * dynamic_pct / 100.0
        traditional_contribution = np.maximum(
            config.monthly_total
            - config.monthly_civislend
            - config.monthly_factoring
            - dynamic_contribution,
            0.0,
        )
        traditional = traditional * traditional_return + traditional_contribution
        staircase = staircase * staircase_return + dynamic_contribution
        civislend = civislend * (1 + civislend_rate) + config.monthly_civislend
        factoring = factoring * (1 + factoring_rate) + config.monthly_factoring
        contributed += config.monthly_total

        if month % 12 == 0:
            annual_return_pct = (rolling_growth - 1.0) * 100.0
            dynamic_pct = np.where(
                annual_return_pct >= config.scale_return_threshold_pct,
                np.minimum(
                    dynamic_pct + config.staircase_step_pct,
                    config.maximum_staircase_pct,
                ),
                dynamic_pct,
            )
            dynamic_pct = np.where(
                annual_return_pct < 0,
                np.maximum(
                    dynamic_pct - config.staircase_step_pct,
                    config.initial_staircase_pct,
                ),
                dynamic_pct,
            )
            rolling_growth.fill(1.0)

        total = civislend + factoring + traditional + staircase
        rows.append(
            {
                "month": month,
                "date": start_timestamp + pd.offsets.MonthEnd(month),
                "contributed": contributed,
                "p10": float(np.percentile(total, 10)),
                "p50": float(np.percentile(total, 50)),
                "p90": float(np.percentile(total, 90)),
                "probability_above_contributions_pct": float(
                    100.0 * np.mean(total > contributed)
                ),
                "average_staircase_allocation_pct": float(np.mean(dynamic_pct)),
            }
        )
    return pd.DataFrame(rows)
