import pandas as pd
import pytest

from src.portfolio_snapshot import (
    compare_portfolio_valuations,
    group_portfolio_snapshot_for_home,
    latest_portfolio_snapshot,
    portfolio_platform_reconciliation,
    reconcile_current_portfolio,
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


def test_declared_and_market_values_remain_separate() -> None:
    declared = pd.DataFrame(
        [
            {"platform": "Broker", "asset_name": "Acción", "analysis_ticker": "AAA", "value_eur": 100.0, "gain_loss_eur": 10.0},
            {"platform": "Banco", "asset_name": "Fondo", "analysis_ticker": "", "value_eur": 200.0, "gain_loss_eur": -5.0},
        ]
    )
    market = declared.copy()
    market.loc[0, "value_eur"] = 108.0
    market.loc[0, "gain_loss_eur"] = 18.0
    market["valuation_status"] = ["Precio actualizado", "Dato manual"]
    market["market_as_of"] = ["2026-08-14", ""]

    comparison = compare_portfolio_valuations(declared, market)

    assert comparison["declared_value_eur"].tolist() == [100.0, 200.0]
    assert comparison["market_value_eur"].tolist() == [108.0, 200.0]
    assert comparison["difference_eur"].tolist() == [8.0, 0.0]
    assert comparison["market_gain_loss_eur"].tolist() == [18.0, -5.0]


def test_platform_reconciliation_reports_real_market_coverage() -> None:
    declared = pd.DataFrame(
        [
            {"platform": "Broker", "asset_name": "A", "value_eur": 100.0, "gain_loss_eur": 5.0},
            {"platform": "Broker", "asset_name": "B", "value_eur": 50.0, "gain_loss_eur": -2.0},
        ]
    )
    market = declared.copy()
    market["value_eur"] = [110.0, 50.0]
    market["gain_loss_eur"] = [15.0, -2.0]
    market["valuation_status"] = ["Precio actualizado", "Dato manual"]

    summary = portfolio_platform_reconciliation(declared, market).iloc[0]

    assert summary["declared_value_eur"] == pytest.approx(150.0)
    assert summary["market_value_eur"] == pytest.approx(160.0)
    assert summary["difference_eur"] == pytest.approx(10.0)
    assert summary["market_priced_count"] == 1
    assert summary["line_count"] == 2


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


def test_snapshot_without_quantity_remains_a_manual_value() -> None:
    positions = pd.DataFrame(
        [
            {
                "snapshot_date": "2026-08-11",
                "platform": "Trade Republic",
                "asset_name": "Nintendo",
                "asset_type": "Acción",
                "analysis_ticker": "NTDOY",
                "quantity": None,
                "currency": "EUR",
                "value_eur": 489.56,
                "cost_estimate_eur": 430.0,
            }
        ]
    )

    refreshed, status = refresh_portfolio_snapshot_prices(
        positions,
        {"NTDOY": 25.0},
        {"EUR": 1.0},
    )

    assert refreshed.iloc[0]["value_eur"] == pytest.approx(489.56)
    assert refreshed.iloc[0]["valuation_status"] == "Dato manual (sin cantidad)"
    assert status.manual_count == 1
    assert status.pending_count == 0


def test_diary_position_replaces_stale_snapshot_without_touching_other_assets() -> None:
    snapshot = pd.DataFrame(
        [
            {
                "snapshot_date": "2026-08-01",
                "platform": "Revolut",
                "asset_name": "Arista Networks",
                "raw_identifier": "ANET",
                "analysis_ticker": "ANET",
                "asset_type": "Acción",
                "portfolio_block": "Cartera actual",
                "quantity": 2.0,
                "current_price": 165.0,
                "currency": "USD",
                "value_eur": 330.0,
                "cost_estimate_eur": 300.0,
                "gain_loss_eur": 30.0,
                "return_pct": 10.0,
            },
            {
                "snapshot_date": "2026-08-01",
                "platform": "Civislend",
                "asset_name": "Proyecto 1",
                "raw_identifier": "",
                "analysis_ticker": "",
                "asset_type": "Crowdlending",
                "portfolio_block": "Alternativas",
                "quantity": None,
                "currency": "EUR",
                "value_eur": 500.0,
                "cost_estimate_eur": 500.0,
                "gain_loss_eur": 0.0,
                "return_pct": 0.0,
            },
        ]
    )
    operations = pd.DataFrame([{"ticker": "ANET", "side": "Compra"}])
    dashboard = pd.DataFrame(
        [
            {
                "ticker": "ANET",
                "currency": "USD",
                "quantity": 0.75,
                "current_price": 190.0,
                "cost_basis_eur": 120.0,
                "net_value_eur": 129.26,
                "net_pnl_eur": 9.26,
                "net_return_pct": 7.7167,
            }
        ]
    )

    reconciled = reconcile_current_portfolio(snapshot, operations, dashboard)

    assert len(reconciled) == 2
    anet = reconciled.loc[reconciled["analysis_ticker"] == "ANET"].iloc[0]
    assert anet["quantity"] == pytest.approx(0.75)
    assert anet["value_eur"] == pytest.approx(129.26)
    assert anet["source"] == "Diario de operaciones + último precio"
    assert reconciled.loc[reconciled["platform"] == "Civislend", "value_eur"].iloc[0] == 500.0


def test_closed_diary_position_removes_stale_snapshot_from_current_view() -> None:
    snapshot = pd.DataFrame(
        [
            {
                "snapshot_date": "2026-08-01",
                "platform": "Revolut",
                "asset_name": "Arista Networks",
                "analysis_ticker": "ANET",
                "asset_type": "Acción",
                "value_eur": 330.0,
            }
        ]
    )
    operations = pd.DataFrame(
        [
            {"ticker": "ANET", "side": "Compra"},
            {"ticker": "ANET", "side": "Venta"},
        ]
    )

    reconciled = reconcile_current_portfolio(
        snapshot,
        operations,
        pd.DataFrame(),
    )

    assert reconciled.empty


def test_recent_complete_snapshot_wins_over_older_diary_purchase() -> None:
    snapshot = pd.DataFrame(
        [
            {
                "snapshot_date": "2026-08-13",
                "platform": "Revolut",
                "asset_name": "Oracle",
                "analysis_ticker": "ORCL",
                "asset_type": "Acción",
                "value_eur": 270.0,
            }
        ]
    )
    operations = pd.DataFrame(
        [
            {
                "ticker": "MRNA",
                "side": "Compra",
                "executed_at": "2026-08-01T16:00:00",
            }
        ]
    )
    dashboard = pd.DataFrame(
        [{"ticker": "MRNA", "quantity": 1.0, "net_value_eur": 52.0}]
    )

    reconciled = reconcile_current_portfolio(snapshot, operations, dashboard)

    assert reconciled["analysis_ticker"].tolist() == ["ORCL"]
