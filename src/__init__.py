"""Componentes de Stock Signal Lab."""

from .backtesting import BacktestResult, run_backtest
from .auth import hash_password, verify_password
from .data_loader import DataDownloadError, download_fundamental_snapshot, download_prices
from .fundamentals import FundamentalResult, evaluate_fundamentals
from .indicators import add_indicators
from .opportunity import OpportunityResult, combine_opportunity
from .recommendations import (
    ForwardReturnStudy,
    build_entry_guide,
    build_profit_taking_plan,
    historical_forward_return_study,
)
from .return_calibration import (
    ReturnCalibrationResult,
    annual_rate_to_horizon_return,
    calibrate_score_returns,
)
from .signal_engine import SignalResult, evaluate_latest_signal

__all__ = [
    "BacktestResult",
    "DataDownloadError",
    "FundamentalResult",
    "ForwardReturnStudy",
    "OpportunityResult",
    "ReturnCalibrationResult",
    "SignalResult",
    "add_indicators",
    "annual_rate_to_horizon_return",
    "build_entry_guide",
    "build_profit_taking_plan",
    "calibrate_score_returns",
    "combine_opportunity",
    "download_prices",
    "download_fundamental_snapshot",
    "evaluate_fundamentals",
    "evaluate_latest_signal",
    "historical_forward_return_study",
    "hash_password",
    "run_backtest",
    "verify_password",
]
