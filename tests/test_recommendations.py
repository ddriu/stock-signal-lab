import numpy as np
import pandas as pd
import pytest

from src.recommendations import (
    ForwardReturnStudy,
    build_entry_guide,
    build_profit_taking_plan,
    historical_forward_return_study,
)


def test_forward_study_uses_non_overlapping_events() -> None:
    index = pd.date_range("2020-01-01", periods=80)
    close = np.linspace(100, 180, 80)
    score = np.tile([70, 70, 40, 40], 20)
    frame = pd.DataFrame(
        {
            "close": close,
            "signal_score": score,
            "sma_medium": close - 2,
            "sma_long": close - 5,
            "sma_medium_slope": 1,
        },
        index=index,
    )
    study = historical_forward_return_study(
        frame, current_score=70, horizon_days=5, score_tolerance=2
    )
    assert study.samples > 0
    assert study.median_return_pct is not None
    assert study.median_return_pct > 0


def test_entry_guide_limits_first_purchase_to_half_of_maximum() -> None:
    study = ForwardReturnStudy(20, 10, 5, 6, 60, -2, 12)
    guide = build_entry_guide(
        fundamental_score=85,
        technical_score=82,
        entry_label="Entrada fuerte",
        maximum_position_value=2_000,
        current_price=100,
        study=study,
    )
    assert guide.label == "Entrada escalonada"
    assert guide.initial_amount == 1_000
    assert guide.initial_quantity == 10


def test_profit_plan_sells_no_more_than_half_immediately() -> None:
    plan = build_profit_taking_plan(
        quantity=100,
        average_cost=100,
        current_price=130,
        stop_loss_pct=8,
        fee_per_sale=1,
    )
    assert all(level.reached for level in plan.levels)
    assert plan.suggested_sell_now_pct == 50
    assert plan.suggested_sell_now_quantity == 50
    assert plan.net_profit_if_sold_now == pytest.approx(1_499)
    assert plan.trailing_quantity == 50
