import numpy as np
import pandas as pd

from src.fundamentals import FundamentalResult
from src.opportunity import (
    RelativeStrengthResult,
    RiskResult,
    combine_opportunity,
    evaluate_relative_strength,
    evaluate_risk,
    evaluate_valuation,
)
from src.signal_engine import SignalResult


def _price_frame(multiplier: float = 1.0) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=300, freq="B")
    close = np.linspace(100, 150 * multiplier, len(index))
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(len(index), 1_000_000),
            "atr_14": close * 0.02,
        },
        index=index,
    )


def test_valuation_uses_multiple_independent_metrics() -> None:
    result = evaluate_valuation(
        {
            "forwardPE": 18,
            "trailingPE": 20,
            "pegRatio": 1.2,
            "priceToBook": 2.5,
            "marketCap": 1_000,
            "freeCashflow": 80,
        },
        "TEST",
    )
    assert result.score is not None
    assert result.score >= 75
    assert result.coverage_pct == 100


def test_relative_strength_rewards_outperformance() -> None:
    stock = _price_frame(1.2)
    market = _price_frame(0.8)
    result = evaluate_relative_strength(
        "TEST",
        stock,
        market,
        broad_name="INDEX",
    )
    assert result.score is not None
    assert result.score > 50
    assert result.broad_excess_3m_pct is not None
    assert result.broad_excess_3m_pct > 0


def test_risk_score_is_available_with_price_history() -> None:
    result = evaluate_risk("TEST", _price_frame())
    assert result.score is not None
    assert result.coverage_pct == 100
    assert result.max_drawdown_1y_pct is not None


def test_combined_opportunity_keeps_confidence_separate() -> None:
    fundamentals = FundamentalResult(
        ticker="TEST",
        score=80,
        coverage_pct=80,
        positive_factors=(),
        risk_factors=(),
        country="US",
        sector="Technology",
        currency="USD",
    )
    valuation = evaluate_valuation({"forwardPE": 18, "pegRatio": 1.1}, "TEST")
    signal = SignalResult(
        ticker="TEST",
        as_of=pd.Timestamp("2026-01-02"),
        score=80,
        label="Entrada fuerte",
        position_label="Mantener",
        explanation="",
        positive_factors=(),
        risk_factors=(),
    )
    relative = RelativeStrengthResult(
        ticker="TEST",
        score=75,
        coverage_pct=70,
        broad_benchmark="SPY",
        sector_benchmark="XLK",
        stock_return_3m_pct=15,
        broad_excess_3m_pct=8,
        sector_excess_3m_pct=4,
        positive_factors=(),
        risk_factors=(),
    )
    risk = RiskResult(
        ticker="TEST",
        score=65,
        coverage_pct=100,
        annualized_volatility_pct=25,
        max_drawdown_1y_pct=-15,
        average_turnover_20d=50_000_000,
        atr_pct=2.5,
        positive_factors=(),
        risk_factors=(),
    )
    result = combine_opportunity(
        "TEST",
        fundamentals,
        valuation,
        signal,
        relative,
        risk,
    )
    assert result.score >= 70
    assert result.confidence_pct < 100
    assert result.label in {"Candidata", "Oportunidad destacada"}
