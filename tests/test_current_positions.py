import pandas as pd
import pytest

from src.current_positions import (
    REFERENCE_COST,
    REFERENCE_ENTRY,
    REFERENCE_GAIN,
    REFERENCE_RETURN,
    estimate_current_position,
    snapshot_with_current_position,
    snapshot_without_positions,
)


def test_estimates_cost_from_current_value_and_return() -> None:
    estimate = estimate_current_position(
        current_value_eur=494.08,
        reference_kind=REFERENCE_RETURN,
        reference_value=-9.67,
    )

    assert estimate.cost_estimate_eur == pytest.approx(546.9722129967895)
    assert estimate.gain_loss_eur == pytest.approx(-52.89221299678951)
    assert estimate.return_pct == pytest.approx(-9.67)
    assert estimate.is_estimated


def test_estimates_from_gain_or_invested_amount() -> None:
    gain = estimate_current_position(
        current_value_eur=1_050,
        reference_kind=REFERENCE_GAIN,
        reference_value=50,
    )
    invested = estimate_current_position(
        current_value_eur=1_050,
        reference_kind=REFERENCE_COST,
        reference_value=1_000,
    )

    assert gain.cost_estimate_eur == 1_000
    assert invested.gain_loss_eur == 50
    assert gain.return_pct == invested.return_pct == 5


def test_exact_quantity_and_entry_include_purchase_fee() -> None:
    estimate = estimate_current_position(
        current_value_eur=1_100,
        reference_kind=REFERENCE_ENTRY,
        quantity=10,
        average_entry_price=100,
        buy_fee_eur=1,
    )

    assert estimate.cost_estimate_eur == 1_001
    assert estimate.gain_loss_eur == 99
    assert estimate.current_price == 110
    assert not estimate.is_estimated


def test_entry_price_and_gain_alone_can_infer_quantity_when_value_is_known() -> None:
    estimate = estimate_current_position(
        current_value_eur=1_050,
        reference_kind=REFERENCE_GAIN,
        reference_value=50,
        average_entry_price=100,
    )

    assert estimate.quantity == 10
    assert estimate.current_price == 105


def test_snapshot_clones_latest_date_and_updates_same_ticker() -> None:
    existing = pd.DataFrame(
        [
            {
                "snapshot_date": "2026-08-01",
                "platform": "Broker",
                "asset_name": "Empresa A",
                "analysis_ticker": "AAA",
                "value_eur": 100,
            },
            {
                "snapshot_date": "2026-08-01",
                "platform": "Broker",
                "asset_name": "Empresa B",
                "analysis_ticker": "BBB",
                "value_eur": 200,
            },
        ]
    )
    updated = snapshot_with_current_position(
        existing,
        snapshot_date="2026-08-02",
        position={
            "platform": "Broker",
            "asset_name": "Empresa A renovada",
            "analysis_ticker": "AAA",
            "value_eur": 150,
        },
    )

    assert len(updated) == 2
    assert set(updated["snapshot_date"]) == {"2026-08-02"}
    assert updated.loc[updated["analysis_ticker"] == "AAA", "value_eur"].iloc[0] == 150
    assert updated.loc[updated["analysis_ticker"] == "BBB", "value_eur"].iloc[0] == 200


def test_snapshot_rejects_backdated_current_position() -> None:
    existing = pd.DataFrame(
        [{"snapshot_date": "2026-08-02", "platform": "Broker", "asset_name": "A"}]
    )

    with pytest.raises(ValueError, match="anterior"):
        snapshot_with_current_position(
            existing,
            snapshot_date="2026-08-01",
            position={"platform": "Broker", "asset_name": "B"},
        )


def test_snapshot_without_positions_clones_latest_and_removes_only_selected() -> None:
    existing = pd.DataFrame(
        [
            {
                "snapshot_date": "2026-08-12",
                "platform": "Revolut",
                "asset_name": "Moderna",
                "analysis_ticker": "MRNA",
                "value_eur": 55.0,
            },
            {
                "snapshot_date": "2026-08-12",
                "platform": "Trade Republic",
                "asset_name": "Nintendo",
                "analysis_ticker": "NTDOY",
                "value_eur": 490.0,
            },
        ]
    )

    updated = snapshot_without_positions(
        existing,
        snapshot_date="2026-08-13",
        removed=[("Revolut", "Moderna")],
    )

    assert updated["asset_name"].tolist() == ["Nintendo"]
    assert updated["snapshot_date"].tolist() == ["2026-08-13"]
