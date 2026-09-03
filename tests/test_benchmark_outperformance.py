import numpy as np
import pandas as pd

from src.benchmark_outperformance import (
    StrategyEvidence,
    evaluate_benchmark_outperformance,
)


def _frame(daily_return: float, sessions: int = 1_320) -> pd.DataFrame:
    index = pd.bdate_range("2021-01-04", periods=sessions)
    close = 100.0 * np.cumprod(np.full(sessions, 1.0 + daily_return))
    return pd.DataFrame({"close": close}, index=index)


def _strong_evidence() -> dict[str, StrategyEvidence]:
    return {
        "technical": StrategyEvidence(82, 100),
        "opportunity": StrategyEvidence(78, 85),
        "growth": StrategyEvidence(84, 90),
        "fundamental": StrategyEvidence(80, 90),
        "conviction": StrategyEvidence(83, 80),
        "valuation": StrategyEvidence(68, 75),
        "risk": StrategyEvidence(72, 100),
    }


def test_strong_company_with_relative_leadership_is_candidate_at_all_horizons() -> None:
    result = evaluate_benchmark_outperformance(
        ticker="LEAD",
        stock=_frame(0.0010),
        benchmark=_frame(0.0003),
        strategies=_strong_evidence(),
    )

    assert result.best_horizon is not None
    for key in ("short", "medium", "long"):
        horizon = result.for_horizon(key)
        assert horizon.score is not None and horizon.score >= 65
        assert horizon.excess_return_pct is not None and horizon.excess_return_pct > 0
        assert horizon.status in {"Ventaja fuerte a validar", "Candidata a superar"}


def test_quality_alone_cannot_mark_an_underperformer_as_candidate() -> None:
    result = evaluate_benchmark_outperformance(
        ticker="LAG",
        stock=_frame(0.0001),
        benchmark=_frame(0.0006),
        strategies=_strong_evidence(),
    )

    for key in ("short", "medium", "long"):
        horizon = result.for_horizon(key)
        assert horizon.excess_return_pct is not None and horizon.excess_return_pct < 0
        assert horizon.status not in {"Ventaja fuerte a validar", "Candidata a superar"}


def test_recent_listing_does_not_claim_long_term_evidence() -> None:
    result = evaluate_benchmark_outperformance(
        ticker="NEW",
        stock=_frame(0.0012, sessions=400),
        benchmark=_frame(0.0003, sessions=400),
        strategies=_strong_evidence(),
    )

    long = result.for_horizon("long")
    assert long.period_sessions == 252
    assert long.status == "Historial largo insuficiente"
    assert long.coverage_pct > 0


def test_missing_benchmark_is_reported_as_insufficient() -> None:
    result = evaluate_benchmark_outperformance(
        ticker="NOINDEX",
        stock=_frame(0.0010),
        benchmark=None,
        strategies=_strong_evidence(),
    )

    assert all(item.status == "Datos insuficientes" for item in result.horizons)
    assert all(item.excess_return_pct is None for item in result.horizons)


def test_historical_beat_windows_are_non_overlapping() -> None:
    result = evaluate_benchmark_outperformance(
        ticker="LEAD",
        stock=_frame(0.0010, sessions=1_261),
        benchmark=_frame(0.0003, sessions=1_261),
        strategies=_strong_evidence(),
    )

    short = result.for_horizon("short")
    assert short.period_sessions == 63
    assert short.historical_windows == 20
    assert short.historical_beat_rate_pct == 100.0
