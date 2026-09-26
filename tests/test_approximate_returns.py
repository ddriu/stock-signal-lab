from pathlib import Path

import pandas as pd
import pytest

from src.approximate_returns import build_approximate_return_report


def _operations(*rows: dict[str, object]) -> pd.DataFrame:
    normalized: list[dict[str, object]] = []
    for index, row in enumerate(rows, start=1):
        item = {
            "id": index,
            "ticker": "ABC",
            "account_name": "Revolut",
            "side": "Compra",
            "quantity": 1.0,
            "price": 100.0,
            "fees": 1.0,
            "settlement_amount_eur": None,
            "fee_eur": None,
            "fx_rate_to_eur": None,
            "executed_at": "2026-01-01",
            "notes": "",
            "currency": "EUR",
            "recorded_by": "ddriu",
            "created_at": "2026-01-01",
        }
        item.update(row)
        normalized.append(item)
    return pd.DataFrame(normalized)


def test_empty_report_is_explicitly_zero() -> None:
    report = build_approximate_return_report(
        pd.DataFrame(), {}, {"EUR": 1.0}, year=2026
    )

    assert report.open_positions.empty
    assert report.closed_operations.empty
    assert report.summary.approximate_return_pct is None


def test_complete_sale_uses_broker_totals_and_estimates_tax() -> None:
    operations = _operations(
        {
            "side": "Compra",
            "quantity": 10,
            "settlement_amount_eur": 1_001,
            "fee_eur": 1,
        },
        {
            "side": "Venta",
            "quantity": 10,
            "price": 120,
            "settlement_amount_eur": 1_199,
            "fee_eur": 1,
            "executed_at": "2026-02-01",
        },
    )

    report = build_approximate_return_report(
        operations, {}, {"EUR": 1.0}, tax_rate_pct=20, year=2026
    )

    sale = report.closed_operations.iloc[0]
    assert sale["Has pagado por lo vendido"] == pytest.approx(1_001)
    assert sale["Recibiste"] == pytest.approx(1_199)
    assert sale["Ganancia antes de impuestos"] == pytest.approx(198)
    assert sale["Impuesto aproximado"] == pytest.approx(39.6)
    assert report.summary.realized_after_tax_eur == pytest.approx(158.4)
    assert report.summary.fees_eur == pytest.approx(2)


def test_loss_has_zero_tax_and_keeps_negative_result() -> None:
    report = build_approximate_return_report(
        _operations(
            {"settlement_amount_eur": 101},
            {
                "side": "Venta",
                "settlement_amount_eur": 79,
                "executed_at": "2026-02-01",
            },
        ),
        {},
        {"EUR": 1.0},
        year=2026,
    )

    sale = report.closed_operations.iloc[0]
    assert sale["Ganancia antes de impuestos"] == pytest.approx(-22)
    assert sale["Impuesto aproximado"] == 0
    assert sale["Ganancia neta"] == pytest.approx(-22)


def test_partial_sale_allocates_fifo_cost_and_preserves_open_lot() -> None:
    report = build_approximate_return_report(
        _operations(
            {"quantity": 5, "settlement_amount_eur": 501},
            {
                "quantity": 5,
                "price": 120,
                "settlement_amount_eur": 601,
                "executed_at": "2026-01-02",
            },
            {
                "side": "Venta",
                "quantity": 6,
                "price": 120,
                "settlement_amount_eur": 719,
                "executed_at": "2026-02-01",
            },
        ),
        {"ABC": 130},
        {"EUR": 1.0},
        sell_fee_eur=0,
        spread_pct=0,
        fx_cost_pct=0,
        year=2026,
    )

    sale = report.closed_operations.iloc[0]
    opened = report.open_positions.iloc[0]
    assert sale["Has pagado por lo vendido"] == pytest.approx(621.2)
    assert opened["Cantidad"] == pytest.approx(4)
    assert opened["Has pagado"] == pytest.approx(480.8)
    assert opened["Ahora vale"] == pytest.approx(520)


def test_exact_settlement_does_not_count_fee_twice() -> None:
    report = build_approximate_return_report(
        _operations(
            {
                "quantity": 2,
                "price": 50,
                "fees": 5,
                "settlement_amount_eur": 105,
                "fee_eur": 5,
            }
        ),
        {"ABC": 60},
        {"EUR": 1.0},
        sell_fee_eur=0,
        spread_pct=0,
        fx_cost_pct=0,
        year=2026,
    )

    opened = report.open_positions.iloc[0]
    assert opened["Has pagado"] == pytest.approx(105)
    assert opened["Ganancia antes de impuestos"] == pytest.approx(15)
    assert report.summary.fees_eur == pytest.approx(5)


def test_foreign_operation_prefers_recorded_eur_settlement() -> None:
    report = build_approximate_return_report(
        _operations(
            {
                "currency": "USD",
                "quantity": 2,
                "price": 100,
                "settlement_amount_eur": 190,
                "fee_eur": 0.95,
            }
        ),
        {"ABC": 110},
        {"EUR": 1.0, "USD": 1.25},
        sell_fee_eur=0,
        spread_pct=0,
        fx_cost_pct=0,
        year=2026,
    )

    opened = report.open_positions.iloc[0]
    assert opened["Has pagado"] == pytest.approx(190)
    assert opened["Ahora vale"] == pytest.approx(176)
    assert opened["Ganancia antes de impuestos"] == pytest.approx(-14)
    assert opened["Impuesto aproximado"] == 0


def test_registered_fx_is_used_before_current_rate() -> None:
    report = build_approximate_return_report(
        _operations(
            {
                "currency": "USD",
                "quantity": 2,
                "price": 100,
                "fees": 0,
                "fx_rate_to_eur": 0.9,
            }
        ),
        {"ABC": 100},
        {"EUR": 1.0, "USD": 2.0},
        sell_fee_eur=0,
        spread_pct=0,
        fx_cost_pct=0,
        year=2026,
    )

    assert report.open_positions.iloc[0]["Has pagado"] == pytest.approx(180)


def test_open_position_includes_estimated_exit_costs_and_potential_tax() -> None:
    report = build_approximate_return_report(
        _operations({"quantity": 10, "settlement_amount_eur": 1_000}),
        {"ABC": 120},
        {"EUR": 1.0},
        sell_fee_eur=1,
        spread_pct=1,
        fx_cost_pct=0,
        tax_rate_pct=20,
        year=2026,
    )

    opened = report.open_positions.iloc[0]
    assert opened["Coste de vender"] == pytest.approx(13)
    assert opened["Recibirías si vendieras"] == pytest.approx(1_187)
    assert opened["Impuesto aproximado"] == pytest.approx(37.4)
    assert opened["Capital neto disponible"] == pytest.approx(1_149.6)


def test_missing_operation_and_unpriced_position_are_reported() -> None:
    report = build_approximate_return_report(
        _operations(
            {"ticker": "BAD", "price": None, "settlement_amount_eur": None},
            {"ticker": "NOPRICE", "settlement_amount_eur": 100},
        ),
        {},
        {"EUR": 1.0},
        year=2026,
    )

    assert report.summary.incomplete_operations == 1
    assert report.summary.unpriced_positions == 1
    assert report.missing_prices == ("NOPRICE",)


def test_combined_return_uses_closed_and_open_cost_without_double_counting() -> None:
    report = build_approximate_return_report(
        _operations(
            {"ticker": "CLOSED", "settlement_amount_eur": 100},
            {
                "ticker": "CLOSED",
                "side": "Venta",
                "settlement_amount_eur": 120,
                "executed_at": "2026-02-01",
            },
            {"ticker": "OPEN", "settlement_amount_eur": 200},
        ),
        {"OPEN": 220},
        {"EUR": 1.0},
        tax_rate_pct=0,
        sell_fee_eur=0,
        spread_pct=0,
        fx_cost_pct=0,
        year=2026,
    )

    assert report.summary.approximate_total_pnl_eur == pytest.approx(40)
    assert report.summary.approximate_return_pct == pytest.approx(40 / 300 * 100)


def test_invalid_assumptions_are_rejected() -> None:
    with pytest.raises(ValueError, match="impuesto"):
        build_approximate_return_report(
            pd.DataFrame(), {}, {"EUR": 1.0}, tax_rate_pct=-1
        )


def test_app_exposes_the_approximate_real_result_tab() -> None:
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text(
        encoding="utf-8"
    )

    assert '"Resultado real aproximado"' in source
    assert "render_approximate_returns(" in source
    assert "opportunity_gain >= 12" in source
    assert "Impuesto potencial" in source
    assert "Capital después de vender" in source
