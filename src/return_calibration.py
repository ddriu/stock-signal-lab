"""Calibración histórica del score para inversiones mantenidas 30 días o más."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

import numpy as np
import pandas as pd


TRADING_SESSIONS_PER_YEAR = 252
MINIMUM_RELIABLE_SAMPLES = 30
SCORE_TIERS: tuple[tuple[str, int, int], ...] = (
    ("Entrada interesante · 65–74", 65, 74),
    ("Entrada fuerte · 75–100", 75, 100),
)


@dataclass(frozen=True)
class ReturnCalibrationResult:
    """Eventos observados y resumen por nivel de score."""

    events: pd.DataFrame
    by_score: pd.DataFrame
    horizon_sessions: int
    sego_target_return_pct: float
    civislend_target_return_pct: float
    minimum_samples: int = MINIMUM_RELIABLE_SAMPLES


def annual_rate_to_horizon_return(
    annual_rate_pct: float,
    horizon_sessions: int,
) -> float:
    """Convierte una rentabilidad anual compuesta al mismo horizonte bursátil."""

    if annual_rate_pct <= -100:
        raise ValueError("La rentabilidad anual debe ser superior a -100%.")
    if horizon_sessions < 1:
        raise ValueError("El horizonte debe contener al menos una sesión.")
    return (
        (1.0 + annual_rate_pct / 100.0)
        ** (horizon_sessions / TRADING_SESSIONS_PER_YEAR)
        - 1.0
    ) * 100.0


def score_tier(score: float | int) -> str | None:
    """Devuelve el nivel de entrada utilizado en los avisos de la aplicación."""

    value = int(score)
    for label, lower, upper in SCORE_TIERS:
        if lower <= value <= upper:
            return label
    return None


def _wilson_interval(successes: int, samples: int) -> tuple[float, float]:
    """Intervalo Wilson del 95% para no presentar una tasa como certeza."""

    if samples <= 0:
        return float("nan"), float("nan")
    z = 1.959963984540054
    proportion = successes / samples
    denominator = 1.0 + z * z / samples
    centre = (proportion + z * z / (2.0 * samples)) / denominator
    margin = (
        z
        * sqrt(
            proportion * (1.0 - proportion) / samples
            + z * z / (4.0 * samples * samples)
        )
        / denominator
    )
    return max(0.0, centre - margin) * 100.0, min(1.0, centre + margin) * 100.0


def _empty_result(
    horizon_sessions: int,
    sego_target: float,
    civislend_target: float,
    minimum_samples: int,
) -> ReturnCalibrationResult:
    return ReturnCalibrationResult(
        events=pd.DataFrame(),
        by_score=pd.DataFrame(),
        horizon_sessions=horizon_sessions,
        sego_target_return_pct=sego_target,
        civislend_target_return_pct=civislend_target,
        minimum_samples=minimum_samples,
    )


def calibrate_score_returns(
    frames: dict[str, pd.DataFrame],
    *,
    horizon_sessions: int = 21,
    sego_annual_rate_pct: float = 5.5,
    civislend_annual_rate_pct: float = 10.5,
    position_value: float = 1_000.0,
    fee_per_order: float = 1.0,
    slippage_pct: float = 0.05,
    minimum_samples: int = MINIMUM_RELIABLE_SAMPLES,
) -> ReturnCalibrationResult:
    """Mide qué ocurrió tras cada nueva señal de entrada de las empresas cargadas.

    La señal se conoce al cierre y la compra se simula en la apertura siguiente.
    Se mantiene la inversión durante todo el horizonte para medir una tesis de
    30 días o más, no operaciones intradía. Los eventos de un mismo ticker no se
    solapan, pero los de empresas distintas pueden pertenecer al mismo régimen.
    """

    if horizon_sessions < 1:
        raise ValueError("El horizonte debe contener al menos una sesión.")
    if position_value <= 0:
        raise ValueError("El importe simulado debe ser positivo.")
    if fee_per_order < 0 or slippage_pct < 0:
        raise ValueError("Comisiones y ejecución imperfecta no pueden ser negativas.")
    if position_value <= fee_per_order:
        raise ValueError("El importe simulado debe superar la comisión de compra.")
    if minimum_samples < 1:
        raise ValueError("El mínimo de casos debe ser positivo.")

    sego_target = annual_rate_to_horizon_return(
        sego_annual_rate_pct,
        horizon_sessions,
    )
    civislend_target = annual_rate_to_horizon_return(
        civislend_annual_rate_pct,
        horizon_sessions,
    )
    rows: list[dict[str, object]] = []
    required = {"open", "low", "close", "signal_score", "buy_setup"}
    slippage_rate = slippage_pct / 100.0

    for ticker, source in frames.items():
        if source.empty or not required.issubset(source.columns):
            continue
        data = source.loc[:, sorted(required)].copy().sort_index()
        for column in ("open", "low", "close", "signal_score"):
            data[column] = pd.to_numeric(data[column], errors="coerce")
        data["buy_setup"] = data["buy_setup"].fillna(False).astype(bool)
        data = data.dropna(subset=["open", "low", "close", "signal_score"])
        if data.empty:
            continue

        new_signal = data["buy_setup"] & ~data["buy_setup"].shift(
            1,
            fill_value=False,
        )
        event_positions = np.flatnonzero(new_signal.to_numpy())
        next_allowed = 0
        for signal_position in event_positions:
            entry_position = int(signal_position) + 1
            exit_position = entry_position + horizon_sessions - 1
            if entry_position < next_allowed or exit_position >= len(data):
                continue
            score = int(round(float(data["signal_score"].iloc[signal_position])))
            tier = score_tier(score)
            if tier is None:
                continue

            entry_open = float(data["open"].iloc[entry_position])
            exit_close = float(data["close"].iloc[exit_position])
            if entry_open <= 0 or exit_close <= 0:
                continue
            entry_fill = entry_open * (1.0 + slippage_rate)
            exit_fill = exit_close * (1.0 - slippage_rate)
            quantity = (position_value - fee_per_order) / entry_fill
            final_value = quantity * exit_fill - fee_per_order
            net_return = (final_value / position_value - 1.0) * 100.0
            gross_return = (exit_close / entry_open - 1.0) * 100.0
            holding_lows = data["low"].iloc[entry_position : exit_position + 1]
            maximum_drawdown = (
                (float(holding_lows.min()) / entry_fill - 1.0) * 100.0
                if not holding_lows.empty
                else float("nan")
            )
            rows.append(
                {
                    "ticker": str(ticker).strip().upper(),
                    "signal_date": pd.Timestamp(data.index[signal_position]),
                    "entry_date": pd.Timestamp(data.index[entry_position]),
                    "exit_date": pd.Timestamp(data.index[exit_position]),
                    "score": score,
                    "score_tier": tier,
                    "gross_return_pct": gross_return,
                    "net_return_pct": net_return,
                    "maximum_drawdown_pct": maximum_drawdown,
                    "positive": net_return > 0.0,
                    "beat_sego": net_return > sego_target,
                    "beat_civislend": net_return > civislend_target,
                }
            )
            next_allowed = exit_position + 1

    if not rows:
        return _empty_result(
            horizon_sessions,
            sego_target,
            civislend_target,
            minimum_samples,
        )

    events = pd.DataFrame(rows).sort_values(
        ["signal_date", "ticker"],
        ignore_index=True,
    )
    summaries: list[dict[str, object]] = []
    groups: list[tuple[str, pd.DataFrame]] = [
        (label, events.loc[events["score_tier"] == label])
        for label, _, _ in SCORE_TIERS
    ]
    groups.append(("Todas las entradas · 65+", events))
    for label, group in groups:
        if group.empty:
            continue
        samples = len(group)
        civis_successes = int(group["beat_civislend"].sum())
        confidence_low, confidence_high = _wilson_interval(
            civis_successes,
            samples,
        )
        summaries.append(
            {
                "score_tier": label,
                "samples": samples,
                "enough_evidence": samples >= minimum_samples,
                "median_net_return_pct": float(group["net_return_pct"].median()),
                "mean_net_return_pct": float(group["net_return_pct"].mean()),
                "positive_rate_pct": float(group["positive"].mean() * 100.0),
                "beat_sego_rate_pct": float(group["beat_sego"].mean() * 100.0),
                "beat_civislend_rate_pct": float(
                    group["beat_civislend"].mean() * 100.0
                ),
                "beat_civislend_ci_low_pct": confidence_low,
                "beat_civislend_ci_high_pct": confidence_high,
                "lower_quartile_pct": float(group["net_return_pct"].quantile(0.25)),
                "upper_quartile_pct": float(group["net_return_pct"].quantile(0.75)),
                "median_drawdown_pct": float(
                    group["maximum_drawdown_pct"].median()
                ),
                "worst_decile_drawdown_pct": float(
                    group["maximum_drawdown_pct"].quantile(0.10)
                ),
            }
        )
    by_score = pd.DataFrame(summaries)
    return ReturnCalibrationResult(
        events=events,
        by_score=by_score,
        horizon_sessions=horizon_sessions,
        sego_target_return_pct=sego_target,
        civislend_target_return_pct=civislend_target,
        minimum_samples=minimum_samples,
    )


def calibration_for_score(
    result: ReturnCalibrationResult,
    score: int,
) -> pd.Series | None:
    """Busca la calibración correspondiente a una nota de entrada actual."""

    tier = score_tier(score)
    if tier is None or result.by_score.empty:
        return None
    matched = result.by_score.loc[result.by_score["score_tier"] == tier]
    return matched.iloc[0] if not matched.empty else None
