from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.sector_comparison import compare_sector


def _prices(start: float, daily_growth: float, periods: int = 280) -> pd.DataFrame:
    index = pd.bdate_range("2025-01-01", periods=periods)
    close = start * np.power(1 + daily_growth, np.arange(periods))
    return pd.DataFrame({"close": close}, index=index)


def test_sector_comparison_normalizes_and_ranks_peer_leadership() -> None:
    frames = {
        "FAST": _prices(200, 0.0020),
        "MID": _prices(50, 0.0010),
        "SLOW": _prices(10, -0.0005),
    }

    result = compare_sector(
        frames,
        ["FAST", "MID", "SLOW"],
        horizon_label="6 meses",
    )

    assert result.normalized_prices.iloc[0].dropna().tolist() == pytest.approx(
        [100.0, 100.0, 100.0]
    )
    assert result.metrics.index[0] == "FAST"
    assert result.metrics.loc["FAST", "leadership_score"] > result.metrics.loc[
        "SLOW", "leadership_score"
    ]
    assert result.correlations.shape == (3, 3)


def test_sector_comparison_rejects_invalid_selection() -> None:
    with pytest.raises(ValueError, match="al menos dos"):
        compare_sector({"ONLY": _prices(10, 0.001)}, ["ONLY"])

    frames = {f"T{index}": _prices(10 + index, 0.001) for index in range(11)}
    with pytest.raises(ValueError, match="hasta diez"):
        compare_sector(frames, list(frames))
