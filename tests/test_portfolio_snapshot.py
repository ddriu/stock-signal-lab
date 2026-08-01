import pandas as pd
import pytest

from src.portfolio_snapshot import latest_portfolio_snapshot


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
