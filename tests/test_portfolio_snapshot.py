import pandas as pd
import pytest

from src.portfolio_snapshot import (
    group_portfolio_snapshot_for_home,
    latest_portfolio_snapshot,
    refresh_portfolio_snapshot_prices,
)


def test_latest_snapshot_does_not_mix_historical_dates() -> None:
    positions = pd.DataFrame(
        [
            {
                "snapshot_date": "2026-06-30",
                "platform": "Broker",
                "asset_name": "Anterior",
                "asset_type": "Acción",
                "analysis_ticker": "OLD",
                "value_eur": 999.0,
                "cost_estimate_eur": 800.0,
                "gain_loss_eur": 199.0,
            },
            {
                "snapshot_date": "2026-07-17",
                "platform": "Broker",
                "asset_name": "Acción A",
                "asset_type": "Acción",
                "analysis_ticker": "AAA",
                "value_eur": 600.0,
                "cost_estimate_eur": 500.0,
                "gain_loss_eur": 100.0,
            },
            {
                "snapshot_date": "2026-07-17",
                "platform": "Broker",
                "asset_name": "Efectivo",
                "asset_type": "Efectivo",
                "analysis_ticker": "",
                "value_eur": 25.0,
                "cost_estimate_eur": 25.0,
                "gain_loss_eur": 0.0,
            },
        ]
    )

    latest, summary = latest_portfolio_snapshot(positions)

    assert summary is not None
    assert summary.snapshot_date == "2026-07-17"
    assert summary.value_eur == pytest.approx(625.0)
    assert summary.cost_estimate_eur == pytest.approx(525.0)
    assert summary.gain_loss_eur == pytest.approx(100.0)
    assert summary.return_pct == pytest.approx(100 / 525 * 100)
    assert summary.line_count == 2
    assert summary.investment_count == 1
    assert summary.analyzable_count == 1
    assert latest["asset_name"].tolist() == ["Acción A", "Efectivo"]


def test_latest_snapshot_handles_empty_data() -> None:
    latest, summary = latest_portfolio_snapshot(pd.DataFrame())

    assert latest.empty
    assert summary is None


def test_home_groups_civislend_projects_without_losing_the_total() -> None:
    positions = pd.DataFrame(
        [
            {
                "snapshot_date": "2026-08-03",
                "platform": "Civislend",
                "asset_name": "Proyecto 1",
                "asset_type": "Crowdlending inmobiliario",
                "analysis_ticker": "",
                "value_eur": 500.0,
                "cost_estimate_eur": 480.0,
                "gain_loss_eur": 20.0,
                "return_pct": 20 / 480 * 100,
            },
            {
                "snapshot_date": "2026-08-03",
                "platform": "Civislend",
                "asset_name": "Proyecto 2",
                "asset_type": "Crowdlending inmobiliario",
                "analysis_ticker": "",
                "value_eur": 250.0,
                "cost_estimate_eur": 250.0,
                "gain_loss_eur": 0.0,
                "return_pct": 0.0,
            },
            {
                "snapshot_date": "2026-08-03",
                "platform": "Segofactoring",
                "asset_name": "Sego Factoring",
                "asset_type": "Factoring",
                "analysis_ticker": "",
                "value_eur": 1_850.0,
                "cost_estimate_eur": 1_850.0,
                "gain_loss_eur": 0.0,
                "return_pct": 0.0,
            },
            {
                "snapshot_date": "2026-08-03",
                "platform": "Revolut",
                "asset_name": "Mastercard",
                "asset_type": "Acción",
                "analysis_ticker": "MA",
                "value_eur": 115.0,
                "cost_estimate_eur": 110.0,
                "gain_loss_eur": 5.0,
                "return_pct": 5 / 110 * 100,
            },
        ]
    )

    grouped = group_portfolio_snapshot_for_home(positions)

    assert len(grouped) == 3
    civislend = grouped.loc[grouped["platform"] == "Civislend"].iloc[0]
    assert civislend["asset_name"] == "Civislend · total invertido (2 proyectos)"
    assert civislend["value_eur"] == pytest.approx(750.0)
    assert civislend["cost_estimate_eur"] == pytest.approx(730.0)
    assert civislend["gain_loss_eur"] == pytest.approx(20.0)
    assert civislend["return_pct"] == pytest.approx(20 / 730 * 100)
    assert positions.loc[positions["platform"] == "Civislend", "asset_name"].tolist() == [
        "Proyecto 1",
        "Proyecto 2",
    ]


def test_home_grouping_keeps_empty_frames_unchanged() -> None:
    grouped = group_portfolio_snapshot_for_home(pd.DataFrame())

    assert grouped.empty


def test_snapshot_refreshes_listed_assets_and_keeps_manual_investments() -> None:
    positions = pd.DataFrame(
        [
            {
                "snapshot_date": "2026-08-03",
                "platform": "Revolut",
                "asset_name": "Mastercard",
                "asset_type": "Acción",
                "analysis_ticker": "MA",
                "quantity": 0.25,
                "currency": "USD",
                "current_price": 500.0,
                "value_eur": 110.0,
                "cost_estimate_eur": 105.0,
                "gain_loss_eur": 5.0,
                "return_pct": 5 / 105 * 100,
            },
            {
                "snapshot_date": "2026-08-03",
                "platform": "Civislend",
                "asset_name": "Proyecto 1",
                "asset_type": "Crowdlending",
                "analysis_ticker": "",
                "quantity": None,
                "currency": "EUR",
                "value_eur": 500.0,
                "cost_estimate_eur": 500.0,
                "gain_loss_eur": 0.0,
                "return_pct": 0.0,
            },
        ]
    )

    refreshed, status = refresh_portfolio_snapshot_prices(
        positions,
        {"MA": 600.0},
        {"EUR": 1.0, "USD": 1.2},
        price_dates={"MA": "2026-08-07"},
    )

    mastercard = refreshed.loc[refreshed["analysis_ticker"] == "MA"].iloc[0]
    civislend = refreshed.loc[refreshed["platform"] == "Civislend"].iloc[0]
    assert mastercard["value_eur"] == pytest.approx(125.0)
    assert mastercard["gain_loss_eur"] == pytest.approx(20.0)
    assert mastercard["valuation_status"] == "Precio actualizado"
    assert civislend["value_eur"] == pytest.approx(500.0)
    assert civislend["valuation_status"] == "Dato manual"
    assert status.market_priced_count == 1
    assert status.manual_count == 1
    assert status.pending_count == 0
    assert status.market_as_of == "2026-08-07"


def test_snapshot_refresh_resolves_broker_market_aliases() -> None:
    positions = pd.DataFrame(
        [
            {
                "snapshot_date": "2026-08-11",
                "platform": "Revolut",
                "asset_name": "iShares Copper Miners UCITS ETF USD (Acc)",
                "asset_type": "ETF",
                "analysis_ticker": "CEBS",
                "quantity": 7.14,
                "currency": "EUR",
                "current_price": 10.05,
                "value_eur": 71.76,
                "cost_estimate_eur": 68.46,
                "gain_loss_eur": 3.30,
                "return_pct": 4.82,
            }
        ]
    )

    refreshed, status = refresh_portfolio_snapshot_prices(
        positions,
        {"CEBS.DE": 10.25},
        {"EUR": 1.0},
        price_dates={"CEBS.DE": "2026-08-11"},
    )

    assert refreshed.iloc[0]["value_eur"] == pytest.approx(7.14 * 10.25)
    assert refreshed.iloc[0]["valuation_status"] == "Precio actualizado"
    assert status.market_priced_count == 1
    assert status.pending_count == 0
