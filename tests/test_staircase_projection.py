from datetime import date

import pandas as pd
import pytest

from src.staircase_projection import (
    DEFAULT_SCENARIOS,
    ProjectionScenario,
    StaircaseProjectionConfig,
    default_horizons,
    months_until_year_end,
    project_scenario,
    project_scenarios,
    simulate_projection_ranges,
    staircase_allocation_pct,
    summarize_projection,
)


def _config() -> StaircaseProjectionConfig:
    return StaircaseProjectionConfig(start_date=date(2026, 8, 4))


def test_default_ddriu_plan_allocates_one_thousand_per_month() -> None:
    config = _config()
    result = project_scenario(
        config,
        ProjectionScenario("Central 10%", 10.0),
        months=12,
    )
    first = result.iloc[0]
    assert config.initial_total == pytest.approx(7_750)
    assert first["contributed"] == pytest.approx(8_750)
    assert first["staircase_allocation_pct"] == pytest.approx(20)
    assert result.iloc[-1]["contributed"] == pytest.approx(19_750)
    assert result.iloc[-1]["total_value"] > result.iloc[-1]["contributed"]


def test_staircase_only_expands_when_threshold_is_reached() -> None:
    config = _config()
    assert staircase_allocation_pct(
        config, completed_years=3, staircase_return_pct=6
    ) == pytest.approx(20)
    assert staircase_allocation_pct(
        config, completed_years=1, staircase_return_pct=10
    ) == pytest.approx(25)
    assert staircase_allocation_pct(
        config, completed_years=10, staircase_return_pct=20
    ) == pytest.approx(40)


def test_projection_separates_contributions_and_estimated_profit() -> None:
    projections = project_scenarios(_config(), DEFAULT_SCENARIOS, months=120)
    summary = summarize_projection(projections, default_horizons(date(2026, 8, 4)))
    assert summary["Horizonte"].tolist() == [
        "Diciembre 2026",
        "12 meses",
        "24 meses",
        "36 meses",
        "48 meses",
        "10 años",
    ]
    assert summary.loc[summary["Horizonte"] == "Diciembre 2026", "Meses"].iloc[0] == 5
    assert summary.loc[summary["Horizonte"] == "10 años", "Total aportado"].iloc[0] == pytest.approx(
        127_750
    )
    assert summary["Excepcional 20%"].iloc[-1] > summary["Central 10%"].iloc[-1]


def test_monte_carlo_is_reproducible_and_orders_percentiles() -> None:
    first = simulate_projection_ranges(
        _config(), simulations=300, months=24, seed=7
    )
    second = simulate_projection_ranges(
        _config(), simulations=300, months=24, seed=7
    )
    pd.testing.assert_frame_equal(first, second)
    assert (first["p10"] <= first["p50"]).all()
    assert (first["p50"] <= first["p90"]).all()
    assert first["probability_above_contributions_pct"].between(0, 100).all()


def test_invalid_fixed_contributions_are_rejected() -> None:
    with pytest.raises(ValueError):
        StaircaseProjectionConfig(
            start_date=date(2026, 8, 4),
            monthly_total=400,
            monthly_civislend=250,
            monthly_factoring=250,
        ).validate()


def test_months_until_year_end_includes_current_month() -> None:
    assert months_until_year_end(date(2026, 8, 4)) == 5
    assert months_until_year_end(date(2026, 12, 31)) == 1
