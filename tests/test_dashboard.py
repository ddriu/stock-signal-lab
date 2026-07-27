import pandas as pd
import pytest

from src.dashboard import build_position_dashboard
from src.journal import calculate_open_positions


def test_dashboard_calculates_net_returns_fees_and_allocation() -> None:
    operations = pd.DataFrame(
        [
            {
                "id": 1,
                "ticker": "ABC",
                "side": "Compra",
                "quantity": 10.0,
                "price": 100.0,
                "fees": 1.0,
                "executed_at": "2025-01-01",
                "notes": "",
                "currency": "EUR",
                "created_at": "2025-01-01",
            },
            {
                "id": 2,
                "ticker": "ABC",
                "side": "Venta",
                "quantity": 2.0,
                "price": 120.0,
                "fees": 1.0,
                "executed_at": "2025-02-01",
                "notes": "",
                "currency": "EUR",
                "created_at": "2025-02-01",
            },
        ]
    )
    positions = calculate_open_positions(operations)

    dashboard, kpis = build_position_dashboard(
        operations,
        positions,
        {"ABC": 125.0},
        {"EUR": 1.0},
        sell_fee_eur=1.0,
    )

    assert kpis.operations_count == 2
    assert kpis.open_positions_count == 1
    assert kpis.priced_positions_count == 1
    assert kpis.fees_eur == pytest.approx(2.0)
    assert kpis.realized_pnl_eur == pytest.approx(38.8)
    assert kpis.unrealized_pnl_eur == pytest.approx(198.2)
    assert dashboard.iloc[0]["allocation_pct"] == pytest.approx(100.0)


def test_dashboard_keeps_unpriced_position_visible() -> None:
    operations = pd.DataFrame(
        [
            {
                "id": 1,
                "ticker": "ABC",
                "side": "Compra",
                "quantity": 1.0,
                "price": 10.0,
                "fees": 0.0,
                "executed_at": "2025-01-01",
                "notes": "",
                "currency": "EUR",
                "created_at": "2025-01-01",
            }
        ]
    )
    positions = calculate_open_positions(operations)

    dashboard, kpis = build_position_dashboard(
        operations,
        positions,
        {},
        {"EUR": 1.0},
    )

    assert dashboard.iloc[0]["ticker"] == "ABC"
    assert pd.isna(dashboard.iloc[0]["current_price"])
    assert kpis.invested_eur == pytest.approx(10.0)
    assert kpis.priced_positions_count == 0
