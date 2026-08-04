import pandas as pd

from src.visualization import (
    portfolio_snapshot_allocation_chart,
    portfolio_snapshot_assets_chart,
    private_investments_chart,
    staircase_projection_chart,
    staircase_range_chart,
)


def test_long_chart_titles_wrap_and_axes_use_automatic_margins() -> None:
    investments = pd.DataFrame(
        [
            {
                "platform": "Segofactoring",
                "invested_amount": 1_000.0,
                "current_value": 1_050.0,
                "status": "Activa",
            }
        ]
    )
    figure = private_investments_chart(investments)
    assert "<br>" in figure.layout.title.text
    assert figure.layout.xaxis.automargin is True
    assert figure.layout.yaxis.automargin is True
    assert figure.layout.margin.l >= 28
    assert figure.layout.margin.r >= 28


def test_long_asset_names_and_outside_values_are_not_clipped() -> None:
    positions = pd.DataFrame(
        [
            {
                "asset_name": "MSCI Emerging Markets ex China UCITS ETF USD (Acc)",
                "platform": "Trade Republic",
                "value_eur": 210.94,
            },
            {
                "asset_name": "iShares Copper Miners UCITS ETF USD (Acc)",
                "platform": "Revolut",
                "value_eur": 28.77,
            },
        ]
    )
    figure = portfolio_snapshot_assets_chart(positions)
    assert figure.layout.yaxis.automargin is True
    assert figure.data[0].cliponaxis is False


def test_pie_labels_can_expand_their_margins() -> None:
    positions = pd.DataFrame(
        [
            {"platform": "Plataforma con nombre muy largo", "value_eur": 900.0},
            {"platform": "Otra plataforma", "value_eur": 100.0},
        ]
    )
    figure = portfolio_snapshot_allocation_chart(positions)
    assert figure.data[0].automargin is True


def test_staircase_charts_distinguish_contributions_and_uncertainty() -> None:
    dates = pd.date_range("2026-08-31", periods=3, freq="ME")
    projections = pd.DataFrame(
        {
            "scenario": ["Central 10%"] * 3,
            "date": dates,
            "contributed": [8_750, 9_750, 10_750],
            "total_value": [8_780, 9_820, 10_880],
        }
    )
    simulation = pd.DataFrame(
        {
            "date": dates,
            "contributed": [8_750, 9_750, 10_750],
            "p10": [8_600, 9_400, 10_100],
            "p50": [8_780, 9_820, 10_880],
            "p90": [8_950, 10_200, 11_650],
        }
    )
    scenario_figure = staircase_projection_chart(projections)
    range_figure = staircase_range_chart(simulation)
    assert [trace.name for trace in scenario_figure.data] == [
        "Capital aportado",
        "Central 10%",
    ]
    assert range_figure.data[1].fill == "tonexty"
    assert range_figure.data[-1].name == "Capital aportado"
