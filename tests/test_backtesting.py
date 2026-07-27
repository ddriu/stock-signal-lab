import pandas as pd

from config import BacktestConfig, StrategyConfig
from src.backtesting import run_backtest


def test_signal_is_executed_at_next_open() -> None:
    index = pd.date_range("2024-01-01", periods=7, freq="D")
    frame = pd.DataFrame(
        {
            "open": [10, 20, 21, 22, 30, 30, 30],
            "high": [10, 20, 21, 22, 30, 30, 30],
            "low": [10, 20, 21, 22, 30, 30, 30],
            "close": [10, 20, 21, 22, 30, 30, 30],
            "signal_score": [100, 60, 60, 20, 20, 20, 20],
            "buy_setup": [True, False, False, False, False, False, False],
            "sell_setup": [False, False, False, True, True, True, True],
            "reduce_setup": [False, False, False, False, False, False, False],
        },
        index=index,
    )
    strategy = StrategyConfig(
        sma_short=1,
        sma_medium=2,
        sma_long=3,
        stop_loss_pct=50,
        trailing_stop_pct=0,
        max_risk_per_trade_pct=50,
        exit_on_reduce=False,
    )
    result = run_backtest(frame, strategy, BacktestConfig(1_000, 0, 0))
    trade = result.trades.iloc[0]
    assert trade["entry_date"] == index[1]
    assert trade["entry_price"] == 20
    assert trade["exit_date"] == index[4]
    assert trade["exit_price"] == 30
    assert result.metrics["total_return_pct"] == 50


def test_new_bar_high_does_not_retroactively_move_trailing_stop() -> None:
    index = pd.date_range("2024-01-01", periods=7, freq="D")
    frame = pd.DataFrame(
        {
            "open": [10, 10, 20, 20, 20, 20, 20],
            "high": [10, 20, 20, 20, 20, 20, 20],
            "low": [10, 9.5, 17, 20, 20, 20, 20],
            "close": [10, 19, 20, 20, 20, 20, 20],
            "signal_score": [100, 60, 60, 60, 60, 60, 60],
            "buy_setup": [True, False, False, False, False, False, False],
            "sell_setup": [False] * 7,
            "reduce_setup": [False] * 7,
        },
        index=index,
    )
    strategy = StrategyConfig(
        sma_short=1,
        sma_medium=2,
        sma_long=3,
        stop_loss_pct=50,
        trailing_stop_pct=10,
        max_risk_per_trade_pct=50,
        exit_on_reduce=False,
    )
    result = run_backtest(frame, strategy, BacktestConfig(1_000, 0, 0))
    trade = result.trades.iloc[0]
    assert trade["entry_date"] == index[1]
    assert trade["exit_date"] == index[2]
    assert trade["exit_price"] == 18
    assert trade["exit_reason"] == "Trailing stop"
