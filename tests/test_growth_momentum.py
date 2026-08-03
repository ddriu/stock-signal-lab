import numpy as np
import pandas as pd
import pytest

from config import StrategyConfig
from src.growth_momentum import (
    GrowthMomentumConfig,
    calculate_growth_position_plan,
    classify_sector_profile,
    evaluate_growth_momentum,
)
from src.indicators import add_indicators
from src.opportunity import evaluate_relative_strength, evaluate_risk


def _frame(start: float = 100.0, end: float = 180.0) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=320, freq="B")
    close = np.linspace(start, end, len(index))
    raw = pd.DataFrame(
        {
            "open": close * 0.998,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(len(index), 500_000.0),
        },
        index=index,
    )
    return add_indicators(raw, StrategyConfig())


def _evaluate():
    stock = _frame(100, 190)
    market = _frame(100, 135)
    relative = evaluate_relative_strength(
        "TEST",
        stock,
        market,
        broad_name="SPY",
    )
    risk = evaluate_risk("TEST", stock)
    result = evaluate_growth_momentum(
        ticker="TEST",
        frame=stock,
        info={
            "sector": "Technology",
            "industry": "Software",
            "marketCap": 10_000_000_000,
            "revenueGrowth": 0.28,
            "earningsGrowth": 0.32,
            "operatingMargins": 0.24,
            "returnOnEquity": 0.25,
            "freeCashflow": 500_000_000,
        },
        relative=relative,
        risk=risk,
        broad_market=market,
        config=GrowthMomentumConfig(),
    )
    return result


def test_sector_profiles_distinguish_binary_and_fund_assets() -> None:
    assert classify_sector_profile({"industry": "Biotechnology"}).key == "biotech"
    assert classify_sector_profile({"sector": "Energy", "industry": "Uranium"}).key == "energy"
    assert classify_sector_profile({"quoteType": "ETF"}).key == "etf"


def test_growth_momentum_keeps_three_scores_separate() -> None:
    result = _evaluate()
    assert result.growth_score is not None and result.growth_score >= 80
    assert result.momentum_score >= 65
    assert result.context_score >= 50
    assert result.score >= 75
    assert result.label in {"Entrada candidata", "Entrada fuerte"}
    assert result.sector_key == "technology"


def test_quick_scan_cannot_be_presented_as_a_complete_entry() -> None:
    stock = _frame(100, 190)
    market = _frame(100, 135)
    result = evaluate_growth_momentum(
        ticker="QUICK",
        frame=stock,
        info={"symbol": "QUICK", "_quick_mode": True},
        relative=evaluate_relative_strength(
            "QUICK", stock, market, broad_name="SPY"
        ),
        risk=evaluate_risk("QUICK", stock),
        broad_market=market,
        config=GrowthMomentumConfig(),
    )
    assert result.growth_score is None
    assert result.label == "Pendiente de fundamentales"


def test_small_cap_reduces_risk_and_position_limit() -> None:
    stock = _frame()
    market = _frame(100, 130)
    result = evaluate_growth_momentum(
        ticker="SMALL",
        frame=stock,
        info={
            "sector": "Technology",
            "marketCap": 500_000_000,
            "revenueGrowth": 0.20,
            "earningsGrowth": 0.20,
            "operatingMargins": 0.15,
            "returnOnEquity": 0.15,
            "freeCashflow": 10,
        },
        relative=evaluate_relative_strength(
            "SMALL", stock, market, broad_name="SPY"
        ),
        risk=evaluate_risk("SMALL", stock),
        broad_market=market,
        config=GrowthMomentumConfig(),
    )
    assert result.is_small_cap
    assert result.max_position_pct == 3.0
    assert result.suggested_risk_pct < 0.5


def test_position_plan_respects_monthly_budget_and_strategy_cap() -> None:
    result = _evaluate()
    plan = calculate_growth_position_plan(
        result=result,
        config=GrowthMomentumConfig(monthly_allocation_pct=20, strategy_cap_pct=15),
        liquid_capital=10_000,
        monthly_investable=1_000,
        current_strategy_value=0,
    )
    assert plan.monthly_strategy_budget == pytest.approx(200)
    assert plan.remaining_strategy_capacity == pytest.approx(1_500)
    assert plan.remaining_sector_capacity == pytest.approx(300)
    assert plan.remaining_open_risk == pytest.approx(200)
    assert plan.suggested_position_value <= 200
    assert plan.loss_at_stop <= plan.risk_budget
    assert plan.round_trip_commission == 2


def test_position_plan_stops_when_sector_or_open_risk_is_exhausted() -> None:
    result = _evaluate()
    config = GrowthMomentumConfig(
        monthly_allocation_pct=20,
        strategy_cap_pct=15,
        max_sector_pct=20,
        max_open_risk_pct=2,
    )
    sector_full = calculate_growth_position_plan(
        result=result,
        config=config,
        liquid_capital=10_000,
        monthly_investable=1_000,
        current_sector_value=300,
    )
    risk_full = calculate_growth_position_plan(
        result=result,
        config=config,
        liquid_capital=10_000,
        monthly_investable=1_000,
        current_open_risk=200,
    )
    assert sector_full.suggested_position_value == 0
    assert risk_full.suggested_position_value == 0


def test_invalid_threshold_order_is_rejected() -> None:
    with pytest.raises(ValueError):
        GrowthMomentumConfig(
            watch_score=80,
            candidate_score=75,
            strong_score=82,
        ).validate()
