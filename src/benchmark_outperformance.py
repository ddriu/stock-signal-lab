"""Ranking explicable de posible ventaja frente al S&P 500.

El módulo no intenta predecir una rentabilidad futura. Combina evidencia de
fuerza relativa ya observada con las estrategias independientes de la
aplicación y mantiene visible la cobertura de datos. Una empresa sólo se
presenta como candidata cuando el horizonte elegido también muestra ventaja
histórica frente al índice.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import tanh
from typing import Mapping

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class StrategyEvidence:
    """Nota y cobertura de una estrategia existente de la aplicación."""

    score: int | float | None
    coverage_pct: int | float = 100


@dataclass(frozen=True)
class HorizonDefinition:
    key: str
    label: str
    description: str
    periods: tuple[tuple[int, float], ...]
    preferred_period: int
    relative_scale_pct: float
    strategy_weights: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class HorizonAssessment:
    key: str
    label: str
    description: str
    score: int | None
    coverage_pct: int
    status: str
    stock_return_pct: float | None
    benchmark_return_pct: float | None
    excess_return_pct: float | None
    period_sessions: int | None
    historical_beat_rate_pct: float | None
    historical_windows: int
    favorable_strategies: tuple[str, ...]
    explanation: str


@dataclass(frozen=True)
class BenchmarkAssessment:
    ticker: str
    benchmark: str
    horizons: tuple[HorizonAssessment, ...]
    best_horizon: str | None
    best_score: int | None

    def for_horizon(self, key: str) -> HorizonAssessment:
        for assessment in self.horizons:
            if assessment.key == key:
                return assessment
        raise KeyError(key)


HORIZONS: tuple[HorizonDefinition, ...] = (
    HorizonDefinition(
        key="short",
        label="Corto · 1–3 meses",
        description="Prioriza fuerza reciente y una entrada que no llegue tarde.",
        periods=((21, 0.35), (63, 0.45), (126, 0.20)),
        preferred_period=63,
        relative_scale_pct=12.0,
        strategy_weights=(
            ("relative", 45.0),
            ("opportunity", 25.0),
            ("technical", 20.0),
            ("risk", 10.0),
        ),
    ),
    HorizonDefinition(
        key="medium",
        label="Medio · 6–12 meses",
        description="Exige liderazgo sostenido, crecimiento y riesgo razonable.",
        periods=((63, 0.20), (126, 0.35), (252, 0.45)),
        preferred_period=252,
        relative_scale_pct=25.0,
        strategy_weights=(
            ("relative", 35.0),
            ("growth", 30.0),
            ("fundamental", 20.0),
            ("risk", 15.0),
        ),
    ),
    HorizonDefinition(
        key="long",
        label="Largo · 3–5 años",
        description="Da más peso a calidad, convicción y valoración; requiere historia larga.",
        periods=((252, 0.20), (756, 0.40), (1260, 0.40)),
        preferred_period=756,
        relative_scale_pct=45.0,
        strategy_weights=(
            ("relative", 25.0),
            ("conviction", 35.0),
            ("fundamental", 20.0),
            ("valuation", 10.0),
            ("risk", 10.0),
        ),
    ),
)


STRATEGY_LABELS = {
    "relative": "Fuerza frente al S&P 500",
    "opportunity": "Oportunidad actual",
    "technical": "Entrada técnica",
    "risk": "Riesgo controlado",
    "growth": "Crecimiento y momentum",
    "fundamental": "Calidad fundamental",
    "conviction": "Convicción 3–5 años",
    "valuation": "Valoración",
}


def _score(value: int | float | None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return float(np.clip(parsed, 0.0, 100.0)) if np.isfinite(parsed) else None


def _coverage(value: int | float | None) -> float:
    parsed = _score(value)
    return parsed if parsed is not None else 0.0


def _close_series(frame: pd.DataFrame | None) -> pd.Series:
    if frame is None or frame.empty or "close" not in frame:
        return pd.Series(dtype=float)
    values = pd.to_numeric(frame["close"], errors="coerce").dropna()
    if values.empty:
        return values
    timestamps = pd.to_datetime(values.index, errors="coerce", utc=True)
    valid = ~timestamps.isna()
    normalized = pd.Series(
        values.to_numpy()[valid],
        index=timestamps[valid].tz_convert(None).normalize(),
        dtype=float,
    )
    return normalized.groupby(level=0).last().sort_index()


def _aligned_closes(
    stock: pd.DataFrame | None,
    benchmark: pd.DataFrame | None,
) -> pd.DataFrame:
    return pd.concat(
        {
            "stock": _close_series(stock),
            "benchmark": _close_series(benchmark),
        },
        axis=1,
        join="inner",
    ).dropna()


def _return(values: pd.Series, sessions: int) -> float | None:
    if len(values) <= sessions:
        return None
    start = float(values.iloc[-sessions - 1])
    end = float(values.iloc[-1])
    if start <= 0 or end <= 0:
        return None
    return (end / start - 1.0) * 100.0


def _relative_score(excess_pct: float, scale_pct: float) -> float:
    """Transforma exceso de rentabilidad sin saturar por un único salto."""

    return float(np.clip(50.0 + 45.0 * tanh(excess_pct / scale_pct), 0.0, 100.0))


def _historical_beat_rate(
    aligned: pd.DataFrame,
    sessions: int,
) -> tuple[float | None, int]:
    """Mide ventanas no solapadas para no inflar artificialmente la muestra."""

    if sessions < 1 or len(aligned) <= sessions:
        return None, 0
    outcomes: list[bool] = []
    end = len(aligned) - 1
    while end - sessions >= 0:
        start = end - sessions
        stock_start = float(aligned["stock"].iloc[start])
        benchmark_start = float(aligned["benchmark"].iloc[start])
        if stock_start > 0 and benchmark_start > 0:
            stock_return = float(aligned["stock"].iloc[end]) / stock_start - 1.0
            benchmark_return = (
                float(aligned["benchmark"].iloc[end]) / benchmark_start - 1.0
            )
            outcomes.append(stock_return > benchmark_return)
        end = start
    if not outcomes:
        return None, 0
    return sum(outcomes) / len(outcomes) * 100.0, len(outcomes)


def _relative_evidence(
    aligned: pd.DataFrame,
    definition: HorizonDefinition,
) -> tuple[
    float | None,
    int,
    float | None,
    float | None,
    float | None,
    int | None,
    float | None,
    int,
]:
    observations: list[tuple[int, float, float, float, float]] = []
    for sessions, weight in definition.periods:
        stock_return = _return(aligned["stock"], sessions)
        benchmark_return = _return(aligned["benchmark"], sessions)
        if stock_return is None or benchmark_return is None:
            continue
        excess = stock_return - benchmark_return
        observations.append(
            (
                sessions,
                weight,
                stock_return,
                benchmark_return,
                excess,
            )
        )
    available_weight = sum(item[1] for item in observations)
    relative = (
        sum(
            item[1] * _relative_score(item[4], definition.relative_scale_pct)
            for item in observations
        )
        / available_weight
        if available_weight
        else None
    )
    coverage = round(available_weight * 100.0)
    chosen = next(
        (item for item in observations if item[0] == definition.preferred_period),
        max(observations, default=None, key=lambda item: item[0]),
    )
    if chosen is None:
        return relative, coverage, None, None, None, None, None, 0
    sessions, _, stock_return, benchmark_return, excess = chosen
    beat_rate, windows = _historical_beat_rate(aligned, sessions)
    return (
        relative,
        coverage,
        stock_return,
        benchmark_return,
        excess,
        sessions,
        beat_rate,
        windows,
    )


def _combine(
    definition: HorizonDefinition,
    relative_score: float | None,
    relative_coverage: int,
    strategies: Mapping[str, StrategyEvidence],
) -> tuple[int | None, int, tuple[str, ...]]:
    total_weight = sum(weight for _, weight in definition.strategy_weights)
    weighted_score = 0.0
    effective_weight = 0.0
    favorable: list[str] = []
    for key, weight in definition.strategy_weights:
        evidence = (
            StrategyEvidence(relative_score, relative_coverage)
            if key == "relative"
            else strategies.get(key, StrategyEvidence(None, 0))
        )
        score = _score(evidence.score)
        coverage = _coverage(evidence.coverage_pct)
        if score is None or coverage <= 0:
            continue
        adjusted_weight = weight * coverage / 100.0
        weighted_score += score * adjusted_weight
        effective_weight += adjusted_weight
        if score >= 65 and coverage >= 40:
            favorable.append(STRATEGY_LABELS.get(key, key))
    combined = round(weighted_score / effective_weight) if effective_weight else None
    coverage_pct = round(effective_weight / total_weight * 100.0) if total_weight else 0
    return combined, coverage_pct, tuple(favorable)


def _status(
    definition: HorizonDefinition,
    score: int | None,
    coverage_pct: int,
    relative_score: float | None,
    relative_coverage: int,
    excess_return_pct: float | None,
    favorable_count: int,
) -> str:
    if score is None or coverage_pct < 45 or excess_return_pct is None:
        return "Datos insuficientes"
    if definition.key == "long" and relative_coverage < 55:
        return "Historial largo insuficiente"
    if (
        score >= 75
        and (relative_score or 0.0) >= 60
        and excess_return_pct > 0
        and favorable_count >= 2
    ):
        return "Ventaja fuerte a validar"
    if score >= 65 and (relative_score or 0.0) >= 55 and excess_return_pct > 0:
        return "Candidata a superar"
    if score >= 55:
        return "Vigilar"
    return "Sin ventaja actual"


def evaluate_benchmark_outperformance(
    *,
    ticker: str,
    stock: pd.DataFrame,
    benchmark: pd.DataFrame | None,
    strategies: Mapping[str, StrategyEvidence],
    benchmark_name: str = "S&P 500 (SPY)",
) -> BenchmarkAssessment:
    """Evalúa tres horizontes con las mismas reglas para toda la lista."""

    aligned = _aligned_closes(stock, benchmark)
    assessments: list[HorizonAssessment] = []
    for definition in HORIZONS:
        (
            relative_score,
            relative_coverage,
            stock_return,
            benchmark_return,
            excess_return,
            period_sessions,
            beat_rate,
            historical_windows,
        ) = _relative_evidence(aligned, definition)
        score, coverage, favorable = _combine(
            definition,
            relative_score,
            relative_coverage,
            strategies,
        )
        status = _status(
            definition,
            score,
            coverage,
            relative_score,
            relative_coverage,
            excess_return,
            len(favorable),
        )
        if excess_return is None:
            explanation = "Falta histórico comparable con el S&P 500."
        elif status in {"Ventaja fuerte a validar", "Candidata a superar"}:
            explanation = (
                f"Superó al índice en {excess_return:+.1f} puntos en la ventana "
                f"disponible y coinciden {len(favorable)} lecturas."
            )
        elif excess_return > 0:
            explanation = (
                f"La ventaja observada es {excess_return:+.1f} puntos, pero la "
                "confirmación conjunta todavía es insuficiente."
            )
        else:
            explanation = (
                f"Quedó {abs(excess_return):.1f} puntos por detrás del índice en la "
                "ventana utilizada."
            )
        assessments.append(
            HorizonAssessment(
                key=definition.key,
                label=definition.label,
                description=definition.description,
                score=score,
                coverage_pct=coverage,
                status=status,
                stock_return_pct=stock_return,
                benchmark_return_pct=benchmark_return,
                excess_return_pct=excess_return,
                period_sessions=period_sessions,
                historical_beat_rate_pct=beat_rate,
                historical_windows=historical_windows,
                favorable_strategies=favorable,
                explanation=explanation,
            )
        )

    eligible = [
        item
        for item in assessments
        if item.score is not None
        and item.coverage_pct >= 45
        and item.status not in {"Datos insuficientes", "Historial largo insuficiente"}
    ]
    best = max(eligible, default=None, key=lambda item: item.score or -1)
    return BenchmarkAssessment(
        ticker=ticker.strip().upper(),
        benchmark=benchmark_name,
        horizons=tuple(assessments),
        best_horizon=best.label if best is not None else None,
        best_score=best.score if best is not None else None,
    )
