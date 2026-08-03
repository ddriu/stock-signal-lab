import pandas as pd

from src.visualization import (
    portfolio_snapshot_allocation_chart,
    portfolio_snapshot_assets_chart,
    private_investments_chart,
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
