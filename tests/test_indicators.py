import numpy as np
import pandas as pd

from config import StrategyConfig
from src.indicators import add_indicators, calculate_rsi


def test_rsi_stays_in_valid_range() -> None:
    close = pd.Series(np.linspace(100, 140, 80) + np.sin(np.arange(80)))
    result = calculate_rsi(close, 14).dropna()
    assert not result.empty
    assert result.between(0, 100).all()


def test_add_indicators_preserves_input_and_adds_columns() -> None:
    index = pd.date_range("2024-01-01", periods=240, freq="D")
    close = np.linspace(100, 180, len(index))
    frame = pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1_000},
        index=index,
    )
    original_columns = list(frame.columns)
    result = add_indicators(frame, StrategyConfig())
    assert list(frame.columns) == original_columns
    assert {
        "sma_medium",
        "sma_long",
        "rsi",
        "macd",
        "volume_average",
        "momentum_short_pct",
        "momentum_medium_pct",
        "breakout",
        "distance_high_pct",
        "volume_ratio",
    }.issubset(result.columns)
    assert result["sma_long"].notna().sum() == 41
