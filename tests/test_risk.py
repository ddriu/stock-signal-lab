from src.risk import calculate_manual_order_plan, calculate_position_plan


def test_position_plan_limits_loss_to_risk_budget() -> None:
    plan = calculate_position_plan(
        capital=10_000,
        entry_price=100,
        stop_loss_pct=8,
        max_risk_pct=1,
    )
    assert plan.stop_price == 92
    assert plan.quantity == 12.5
    assert plan.position_value == 1_250
    assert plan.loss_at_stop == 100
    assert plan.reference_target_2r == 116


def test_manual_order_plan_accepts_whole_shares_and_builds_targets() -> None:
    plan = calculate_manual_order_plan(
        capital=1_000,
        entry_price=100,
        quantity=1,
        stop_loss_pct=8,
        max_risk_pct=1,
        trailing_stop_pct=10,
        fee_per_order=1,
    )

    assert plan.position_value == 100
    assert plan.stop_price == 92
    assert plan.market_loss_at_stop == 8
    assert plan.estimated_loss_with_fees == 10
    assert plan.within_capital
    assert plan.within_risk_budget
    assert not plan.fractional
    assert [target.price for target in plan.targets] == [108, 116, 124]
    assert plan.stop_ladder[-1].stop_price == 112.5
    assert plan.stop_ladder[-1].locked_return_pct == 12.5


def test_manual_order_plan_by_amount_flags_excess_risk_with_fees() -> None:
    plan = calculate_manual_order_plan(
        capital=1_000,
        entry_price=200,
        investment_amount=125,
        stop_loss_pct=8,
        max_risk_pct=1,
        trailing_stop_pct=10,
        fee_per_order=1,
    )

    assert plan.quantity == 0.625
    assert plan.fractional
    assert plan.estimated_loss_with_fees == 12
    assert not plan.within_risk_budget
    assert plan.maximum_position_value_by_risk == 100


def test_manual_order_plan_with_disabled_trailing_keeps_initial_stop() -> None:
    plan = calculate_manual_order_plan(
        capital=1_000,
        entry_price=100,
        quantity=1,
        stop_loss_pct=8,
        max_risk_pct=1,
        trailing_stop_pct=0,
        fee_per_order=0,
    )

    assert {level.stop_price for level in plan.stop_ladder} == {92}
