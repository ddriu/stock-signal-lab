import pandas as pd

from config import StrategyConfig
from src.signal_engine import (
    LABEL_HOLD,
    LABEL_SELL,
    LABEL_STRONG,
    add_signal_columns,
    evaluate_latest_signal,
)


def signal_frame() -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=2)
    return pd.DataFrame(
        {
            "close": [110.0, 112.0],
            "sma_short": [108.0, 109.0],
            "sma_medium": [105.0, 106.0],
            "sma_long": [100.0, 101.0],
            "sma_medium_slope": [1.0, 1.0],
            "rsi": [50.0, 55.0],
            "macd": [0.5, 1.0],
            "macd_signal": [0.6, 0.8],
            "macd_bullish_cross": [False, True],
            "volume_above_average": [False, True],
            "volume_ratio": [0.9, 1.5],
            "distance_sma_short_pct": [1.8, 2.7],
            "momentum_short_pct": [1.0, 5.0],
            "momentum_medium_pct": [2.0, 12.0],
            "macd_hist_rising": [False, True],
            "breakout": [False, True],
            "distance_high_pct": [-5.0, -1.0],
        },
        index=index,
    )


def test_complete_setup_is_strong_entry_with_full_score() -> None:
    config = StrategyConfig(sma_short=5, sma_medium=10, sma_long=20)
    result = add_signal_columns(signal_frame(), config)
    assert result.iloc[-1]["signal_label"] == LABEL_STRONG
    assert result.iloc[-1]["signal_score"] == 100
    signal = evaluate_latest_signal(result, config, ticker="TEST")
    assert signal.label == LABEL_STRONG
    assert signal.position_label == LABEL_HOLD
    assert "probabilística" in signal.explanation


def test_long_average_loss_has_sell_precedence() -> None:
    config = StrategyConfig(sma_short=5, sma_medium=10, sma_long=20)
    frame = signal_frame()
    frame.loc[:, "close"] = [80.0, 79.0]
    frame.loc[:, "sma_medium"] = [100.0, 100.0]
    frame.loc[:, "sma_long"] = [101.0, 101.0]
    result = add_signal_columns(frame, config)
    assert result.iloc[-1]["position_label"] == LABEL_SELL


def test_single_bad_close_does_not_trigger_immediate_sell() -> None:
    config = StrategyConfig(sma_short=5, sma_medium=10, sma_long=20)
    frame = signal_frame()
    frame.loc[frame.index[-1], ["close", "sma_medium", "sma_long"]] = [80.0, 100.0, 101.0]
    result = add_signal_columns(frame, config)
    assert result.iloc[-1]["position_label"] == LABEL_HOLD


def test_macd_cross_and_volume_surge_are_bonuses_not_requirements() -> None:
    config = StrategyConfig(sma_short=5, sma_medium=10, sma_long=20)
    base = signal_frame()
    base.loc[base.index[-1], "macd_bullish_cross"] = False
    base.loc[base.index[-1], "volume_ratio"] = 0.9
    base_score = add_signal_columns(base, config).iloc[-1]["signal_score"]

    confirmed = base.copy()
    confirmed.loc[confirmed.index[-1], "macd_bullish_cross"] = True
    confirmed.loc[confirmed.index[-1], "volume_ratio"] = 1.3
    confirmed_score = add_signal_columns(confirmed, config).iloc[-1]["signal_score"]

    assert confirmed_score - base_score == 7
