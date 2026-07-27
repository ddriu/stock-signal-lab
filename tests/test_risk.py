from src.risk import calculate_position_plan


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
