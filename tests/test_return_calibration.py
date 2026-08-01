from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.return_calibration import (
    annual_rate_to_horizon_return,
    calibrate_score_returns,
    calibration_for_score,
)


def _frame_with_signals(
    *,
    periods: int = 100,
    signal_positions: tuple[int, ...] = (0, 30, 60),
    score: int = 76,
) -> pd.DataFrame:
    index = pd.bdate_range("2024-01-01", periods=periods)
    close = np.linspace(100.0, 140.0, periods)
    buy = np.zeros(periods, dtype=bool)
    buy[list(signal_positions)] = True
    return pd.DataFrame(
        {
            "open": close,
            "low": close * 0.98,
            "close": close,
            "signal_score": np.where(buy, score, 50),
            "buy_setup": buy,
        },
        index=index,
    )


def test_annual_rate_is_compounded_to_same_horizon() -> None:
    assert annual_rate_to_horizon_return(10.5, 252) == pytest.approx(10.5)
    assert annual_rate_to_horizon_return(10.5, 21) == pytest.approx(0.8355, rel=1e-3)


def test_calibration_enters_next_open_and_subtracts_flat_fees() -> None:
    frame = _frame_with_signals(periods=30, signal_positions=(0,))
    frame.loc[frame.index[1], "open"] = 100.0
    frame.loc[frame.index[1:6], "low"] = [99, 98, 97, 99, 100]
    frame.loc[frame.index[5], "close"] = 110.0

    result = calibrate_score_returns(
        {"TEST": frame},
        horizon_sessions=5,
        position_value=1_000,
        fee_per_order=1,
        slippage_pct=0,
        minimum_samples=1,
    )

    event = result.events.iloc[0]
    assert event["entry_date"] == frame.index[1]
    assert event["exit_date"] == frame.index[5]
    assert event["net_return_pct"] == pytest.approx(9.79)
    assert event["maximum_drawdown_pct"] == pytest.approx(-3.0)


def test_calibration_groups_scores_and_marks_evidence_threshold() -> None:
    result = calibrate_score_returns(
        {
            "STRONG": _frame_with_signals(score=80),
            "ENTRY": _frame_with_signals(score=68),
        },
        horizon_sessions=10,
        minimum_samples=5,
    )

    assert len(result.events) == 6
    assert set(result.by_score["score_tier"]) == {
        "Entrada interesante · 65–74",
        "Entrada fuerte · 75–100",
        "Todas las entradas · 65+",
    }
    aggregate = result.by_score.loc[
        result.by_score["score_tier"] == "Todas las entradas · 65+"
    ].iloc[0]
    assert bool(aggregate["enough_evidence"])
    assert aggregate["beat_civislend_rate_pct"] == 100
    assert calibration_for_score(result, 80) is not None
    assert calibration_for_score(result, 55) is None
