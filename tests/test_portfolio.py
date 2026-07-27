import pytest

from src.portfolio import compare_switch, value_holding


def test_holding_value_subtracts_future_sell_fee() -> None:
    result = value_holding(
        quantity=10,
        average_cost=100.1,
        cost_basis=1_001,
        current_price=120,
        sell_fee=1,
    )
    assert result.net_exit_value == 1_199
    assert result.net_pnl == 198
    assert result.net_return_pct == pytest.approx(198 / 1_001 * 100)
    assert result.break_even_price == pytest.approx(100.2)


def test_switch_subtracts_one_euro_on_each_side() -> None:
    result = compare_switch(
        quantity=10,
        current_price=100,
        candidate_price=200,
        current_currency="EUR",
        candidate_currency="EUR",
        sell_fee=1,
        buy_fee=1,
    )
    assert result.compatible_currency
    assert result.cash_after_sale == 999
    assert result.cash_invested == 998
    assert result.candidate_quantity == 4.99
    assert result.immediate_fees == 2
    assert result.fee_hurdle_pct == pytest.approx(0.2)


def test_cross_currency_switch_is_marked_not_comparable() -> None:
    result = compare_switch(
        quantity=10,
        current_price=100,
        candidate_price=200,
        current_currency="EUR",
        candidate_currency="USD",
    )
    assert not result.compatible_currency
    assert result.candidate_quantity == 0


def test_cross_currency_switch_uses_ecb_rates_and_euro_fees() -> None:
    result = compare_switch(
        quantity=10,
        current_price=100,
        candidate_price=120,
        current_currency="EUR",
        candidate_currency="USD",
        sell_fee=1,
        buy_fee=1,
        fx_rates_per_eur={"EUR": 1.0, "USD": 1.2},
        fee_currency="EUR",
        fx_as_of="2026-01-02",
    )
    assert result.compatible_currency
    assert result.conversion_rate == pytest.approx(1.2)
    # Venta: 1000 EUR - 1 EUR = 999 EUR = 1198.8 USD; compra: -1.2 USD.
    assert result.cash_invested == pytest.approx(1197.6)
    assert result.candidate_quantity == pytest.approx(9.98)
    assert result.immediate_fees == 2
