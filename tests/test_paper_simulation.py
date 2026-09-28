from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from src.paper_simulation import (
    PaperAssumptions,
    PaperOrder,
    PaperSnapshot,
    build_paper_rotation_dashboard,
    can_rebalance_on_date,
    compute_paper_scorecard,
    fill_pending_orders,
    mark_to_market,
    propose_paper_orders,
    seed_paper_portfolio,
)


def _seed(*, cash: float = 100.0):
    return seed_paper_portfolio(
        [
            {
                "ticker": "OLD",
                "quantity": 10,
                "price_eur": 100,
                "cost_basis_eur": 900,
                "sector": "Industrials",
            },
            {
                "ticker": "KEEP",
                "quantity": 5,
                "price_eur": 100,
                "sector": "Health Care",
            },
        ],
        cash_eur=cash,
        benchmark_price_eur=500,
        portfolio_id="demo",
        as_of="2026-09-25",
    )


def _dashboard():
    return SimpleNamespace(
        switches=(
            {
                "Origen": "OLD",
                "Alternativa": "BLUE",
                "Sector alternativa": "Technology",
                "Lectura": "Ventaja clara para estudiar",
            },
        ),
        positions=(
            {
                "Ticker": "OLD",
                "Color": "Naranja",
                "Score horizonte": 35,
            },
            {
                "Ticker": "KEEP",
                "Color": "Verde",
                "Score horizonte": 75,
            },
        ),
        candidates=(
            {
                "Ticker": "BLUE",
                "Color": "Azul",
                "Score horizonte": 88,
                "Confianza": 90,
                "Cobertura": 100,
                "Sector": "Technology",
            },
            {
                "Ticker": "WATCH",
                "Color": "Amarillo",
                "Score horizonte": 72,
                "Confianza": 78,
                "Cobertura": 100,
                "Sector": "Consumer",
            },
        ),
    )


def _bars(**prices: float) -> dict[str, pd.DataFrame]:
    index = pd.to_datetime(["2026-09-25", "2026-09-28", "2026-09-29"])
    return {
        ticker: pd.DataFrame({"open": [value - 1, value, value + 5]}, index=index)
        for ticker, value in prices.items()
    }


def test_seed_is_eur_only_deterministic_and_preserves_a_buy_hold_baseline() -> None:
    state = _seed(cash=100)

    assert [position.ticker for position in state.positions] == ["KEEP", "OLD"]
    assert state.initial_nav_eur == 1_600
    assert state.positions == state.initial_positions
    assert state.cash_eur == 100

    with pytest.raises(ValueError, match="convertidos a EUR"):
        seed_paper_portfolio(
            [{"ticker": "USD", "quantity": 1, "price_eur": 100, "currency": "USD"}],
            as_of="2026-09-25",
        )


def test_frozen_position_counts_in_nav_but_never_invents_market_return() -> None:
    state = seed_paper_portfolio(
        [
            {
                "ticker": "MARKET",
                "quantity": 1,
                "price_eur": 100,
                "value_eur": 100,
            },
            {
                "ticker": "MANUAL_ABC",
                "quantity": 1,
                "price_eur": 900,
                "value_eur": 900,
                "cost_basis_eur": 850,
                "valuation_mode": "frozen",
            },
        ],
        benchmark_price_eur=500,
        as_of="2026-09-25",
    )

    snapshot = mark_to_market(
        state,
        {"MARKET": 110, "MANUAL_ABC": 1},
        500,
        as_of="2026-09-28",
    )

    assert state.initial_nav_eur == 1_000
    assert snapshot.holdings_eur == 1_010
    assert snapshot.buy_hold_nav_eur == 1_010
    assert snapshot.unrealized_pnl_eur == 60
    assert snapshot.data_coverage_pct == pytest.approx(110 / 1_010 * 100)


def test_frozen_position_cannot_be_sold_even_with_a_quote_and_pending_order() -> None:
    state = seed_paper_portfolio(
        [
            {
                "ticker": "MANUAL_ABC",
                "quantity": 1,
                "price_eur": 900,
                "value_eur": 900,
                "valuation_mode": "frozen",
            }
        ],
        as_of="2026-09-25",
    )
    order = PaperOrder(
        id="blocked-frozen-sale",
        portfolio_id=state.portfolio_id,
        signal_date=date(2026, 9, 25),
        effective_after=date(2026, 9, 26),
        ticker="MANUAL_ABC",
        side="Venta",
        target_value_eur=100,
        reason="No debe ejecutarse",
    )

    advanced, trades = fill_pending_orders(
        state,
        [order],
        _bars(MANUAL_ABC=900),
        as_of="2026-09-28",
    )

    assert trades == ()
    assert advanced.positions == state.positions
    assert advanced.cash_eur == state.cash_eur


def test_frozen_position_cannot_be_proposed_as_rotation_origin() -> None:
    state = seed_paper_portfolio(
        [
            {
                "ticker": "FROZEN",
                "quantity": 1,
                "price_eur": 500,
                "valuation_mode": "frozen",
            }
        ],
        as_of="2026-09-25",
    )
    dashboard = SimpleNamespace(
        switches=(
            {
                "Origen": "FROZEN",
                "Alternativa": "BLUE",
                "Sector alternativa": "Technology",
                "Lectura": "Prueba",
            },
        ),
        positions=(
            {"Ticker": "FROZEN", "Color": "Rojo", "Score horizonte": 5},
        ),
        candidates=(
            {
                "Ticker": "BLUE",
                "Color": "Azul",
                "Score horizonte": 90,
                "Confianza": 95,
                "Cobertura": 100,
                "Sector": "Technology",
            },
        ),
    )

    assert propose_paper_orders(
        dashboard,
        state,
        as_of="2026-09-28",
        include_challenger=False,
    ) == ()


def test_assumptions_reject_impossible_or_negative_limits() -> None:
    with pytest.raises(ValueError, match="comisión"):
        PaperAssumptions(buy_fee_eur=-1)
    with pytest.raises(ValueError, match="empresa"):
        PaperAssumptions(max_position_pct=30, max_sector_pct=25)
    with pytest.raises(ValueError, match="vigencia"):
        PaperAssumptions(max_order_age_days=0)


def test_pending_order_expires_instead_of_filling_weeks_later() -> None:
    state = _seed()
    order = PaperOrder(
        id="expired-sale",
        portfolio_id=state.portfolio_id,
        signal_date=date(2026, 9, 1),
        effective_after=date(2026, 9, 2),
        ticker="OLD",
        side="Venta",
        target_value_eur=100,
        reason="Señal antigua",
        engine_version=state.engine_version,
    )
    bars = {
        "OLD": pd.DataFrame(
            {"open": [100]},
            index=pd.to_datetime(["2026-09-28"]),
        )
    }

    advanced, trades = fill_pending_orders(
        state,
        [order],
        bars,
        as_of="2026-09-28",
    )

    assert trades == ()
    assert advanced.positions == state.positions
    assert advanced.cash_eur == state.cash_eur
    assert "expired-sale" not in advanced.filled_order_ids


def test_pending_strict_pair_reserves_the_week_until_it_expires() -> None:
    state = _seed()
    pending = propose_paper_orders(
        _dashboard(),
        state,
        as_of="2026-09-28",
        include_challenger=False,
    )

    assert not can_rebalance_on_date(
        state,
        "2026-09-29",
        pending_orders=pending,
    )
    assert (
        propose_paper_orders(
            _dashboard(),
            state,
            as_of="2026-09-29",
            include_challenger=False,
            pending_orders=pending,
        )
        == ()
    )
    assert can_rebalance_on_date(
        state,
        "2026-10-06",
        pending_orders=pending,
    )


def test_filled_strict_rotation_blocks_another_pair_only_in_the_same_iso_week() -> None:
    initial = _seed()
    first_orders = propose_paper_orders(
        _dashboard(),
        initial,
        as_of="2026-09-25",
        include_challenger=False,
    )
    filled, first_trades = fill_pending_orders(
        initial,
        first_orders,
        _bars(OLD=100, BLUE=50),
        as_of="2026-09-28",
    )

    assert first_trades
    assert not can_rebalance_on_date(filled, "2026-09-29")
    assert can_rebalance_on_date(filled, "2026-09-29", scenario="challenger")
    assert (
        propose_paper_orders(
            _dashboard(),
            filled,
            as_of="2026-09-29",
            include_challenger=False,
        )
        == ()
    )
    assert can_rebalance_on_date(filled, "2026-10-05")
    assert propose_paper_orders(
        _dashboard(),
        filled,
        as_of="2026-10-05",
        include_challenger=False,
    )


def test_fill_allows_second_leg_but_not_a_second_strict_pair_in_the_week() -> None:
    initial = _seed()
    first_orders = propose_paper_orders(
        _dashboard(),
        initial,
        as_of="2026-09-25",
        include_challenger=False,
    )
    second_dashboard = _dashboard()
    second_dashboard.switches = (
        {
            "Origen": "OLD",
            "Alternativa": "WATCH",
            "Sector alternativa": "Consumer",
            "Lectura": "Segundo salto de la semana",
        },
    )
    second_orders = propose_paper_orders(
        second_dashboard,
        initial,
        as_of="2026-09-28",
        include_challenger=False,
    )
    after_first, first_trades = fill_pending_orders(
        initial,
        first_orders,
        _bars(OLD=100, BLUE=50),
        as_of="2026-09-28",
    )
    after_second, second_trades = fill_pending_orders(
        after_first,
        second_orders,
        _bars(OLD=100, WATCH=25),
        as_of="2026-09-29",
    )

    assert {trade.side for trade in first_trades} == {"Compra", "Venta"}
    assert len({trade.pair_id for trade in first_trades}) == 1
    assert second_trades == ()
    assert "WATCH" not in {position.ticker for position in after_second.positions}


def test_proposals_are_deterministic_paper_only_and_separate_both_scenarios() -> None:
    state = _seed()
    assumptions = PaperAssumptions(max_daily_turnover_pct=5)

    first = propose_paper_orders(
        _dashboard(), state, assumptions, as_of="2026-09-25"
    )
    second = propose_paper_orders(
        _dashboard(), state, assumptions, as_of="2026-09-25"
    )

    assert first == second
    assert {order.scenario for order in first} == {"strict", "challenger"}
    assert all(order.environment == "paper" for order in first)
    assert all(order.effective_after > order.signal_date for order in first)
    assert all(order.target_value_eur <= state.initial_nav_eur * 0.05 for order in first)
    assert [(order.side, order.ticker) for order in first if order.scenario == "strict"] == [
        ("Venta", "OLD"),
        ("Compra", "BLUE"),
    ]
    assert [(order.side, order.ticker) for order in first if order.scenario == "challenger"] == [
        ("Venta", "OLD"),
        ("Compra", "WATCH"),
    ]


def test_future_decisions_use_the_diverged_paper_book_in_both_scenarios() -> None:
    state = seed_paper_portfolio(
        [
            {
                "ticker": "PAPER",
                "quantity": 1,
                "price_eur": 100,
                "cost_basis_eur": 100,
                "sector": "Industrials",
            }
        ],
        cash_eur=900,
        portfolio_id="paper-only",
        as_of="2026-09-25",
    )
    common = {
        "Confianza datos": 90,
        "Calidad empresa": 80,
        "Valoración": 80,
        "Riesgo controlado": 80,
        "Fecha": date(2026, 9, 25),
        "Moneda": "EUR",
    }
    summary = [
        {
            **common,
            "Ticker": "PAPER",
            "Oportunidad": 25,
            "Momento entrada": 20,
            "Fuerza relativa": 20,
            "Lectura entrada": "Esperar",
            "Si ya la tienes": "Vender",
            "Tesis invalidada": True,
            "Sector": "Industrials",
        },
        {
            **common,
            "Ticker": "BLUE",
            "Oportunidad": 95,
            "Momento entrada": 95,
            "Fuerza relativa": 95,
            "Lectura entrada": "Entrada fuerte",
            "Si ya la tienes": "Mantener",
            "Sector": "Technology",
        },
        {
            **common,
            "Ticker": "WATCH",
            "Oportunidad": 90,
            "Momento entrada": 90,
            "Fuerza relativa": 90,
            "Lectura entrada": "Esperar",
            "Si ya la tienes": "Mantener",
            "Sector": "Consumer",
        },
        {
            **common,
            "Ticker": "REAL",
            "Oportunidad": 10,
            "Momento entrada": 10,
            "Fuerza relativa": 10,
            "Lectura entrada": "Esperar",
            "Si ya la tienes": "Vender",
            "Tesis invalidada": True,
            "Sector": "Energy",
        },
    ]

    dashboard = build_paper_rotation_dashboard(
        summary,
        state,
        ["BLUE", "WATCH"],
        {},
        pair_correlations={("PAPER", "BLUE"): 0.2},
    )
    orders = propose_paper_orders(dashboard, state, as_of="2026-09-25")

    assert [row["Ticker"] for row in dashboard.positions] == ["PAPER"]
    assert dashboard.switches[0]["Origen"] == "PAPER"
    assert all(order.ticker != "REAL" for order in orders)
    assert {
        order.ticker
        for order in orders
        if order.side == "Venta"
    } == {"PAPER"}
    assert {order.scenario for order in orders} == {"strict", "challenger"}


def test_same_sector_rotation_counts_capacity_released_by_the_sale() -> None:
    dashboard = _dashboard()
    dashboard.switches = (
        {
            "Origen": "OLD",
            "Alternativa": "BLUE",
            "Sector alternativa": "Industrials",
            "Lectura": "Rotación dentro del sector",
        },
    )

    orders = propose_paper_orders(
        dashboard,
        _seed(),
        as_of="2026-09-25",
        include_challenger=False,
    )

    assert [(order.side, order.ticker) for order in orders] == [
        ("Venta", "OLD"),
        ("Compra", "BLUE"),
    ]
    assert orders[0].target_value_eur == orders[1].target_value_eur


def test_grey_or_incomplete_candidate_never_becomes_a_challenger() -> None:
    dashboard = _dashboard()
    dashboard.candidates = (
        {
            "Ticker": "STALE",
            "Color": "Gris",
            "Score horizonte": 95,
            "Confianza": 95,
            "Cobertura": 100,
            "Sector": "Technology",
        },
        {
            "Ticker": "PARTIAL",
            "Color": "Amarillo",
            "Score horizonte": 90,
            "Confianza": 90,
            "Cobertura": 50,
            "Sector": "Technology",
        },
    )

    orders = propose_paper_orders(
        dashboard,
        _seed(),
        as_of="2026-09-25",
    )

    assert all(order.scenario == "strict" for order in orders)


def test_signal_cannot_fill_on_t_and_uses_first_open_after_t() -> None:
    state = _seed()
    strict_orders = tuple(
        order
        for order in propose_paper_orders(
            _dashboard(), state, as_of="2026-09-25", include_challenger=False
        )
        if order.scenario == "strict"
    )
    bars = _bars(OLD=110, BLUE=50)

    unchanged, same_day_trades = fill_pending_orders(
        state,
        strict_orders,
        bars,
        as_of="2026-09-25",
    )
    filled, trades = fill_pending_orders(
        unchanged,
        strict_orders,
        bars,
        as_of="2026-09-29",
    )

    assert same_day_trades == ()
    assert unchanged == state
    assert len(trades) == 2
    assert all(trade.filled_at == date(2026, 9, 28) for trade in trades)
    assert {trade.ticker: trade.price for trade in trades} == {"OLD": 110, "BLUE": 50}
    assert filled.cash_eur >= 0


def test_pending_order_never_fills_at_or_before_the_persisted_state_date() -> None:
    state = replace(_seed(), as_of=date(2026, 9, 28))
    order = PaperOrder(
        id="late-data-sale",
        portfolio_id=state.portfolio_id,
        signal_date=date(2026, 9, 25),
        effective_after=date(2026, 9, 26),
        ticker="OLD",
        side="Venta",
        target_value_eur=50,
        reason="La apertura anterior llegó tarde",
        engine_version=state.engine_version,
    )
    bars = {
        "OLD": pd.DataFrame(
            {"open": [100, 101]},
            index=pd.to_datetime(["2026-09-28", "2026-09-29"]),
        )
    }

    advanced, trades = fill_pending_orders(
        state,
        [order],
        bars,
        as_of="2026-09-29",
    )

    assert len(trades) == 1
    assert trades[0].filled_at == date(2026, 9, 29)
    assert trades[0].price == 101
    assert advanced.filled_order_ids == (order.id,)


def test_pending_order_waits_when_only_a_closed_session_open_is_available() -> None:
    state = replace(_seed(), as_of=date(2026, 9, 28))
    order = PaperOrder(
        id="still-pending",
        portfolio_id=state.portfolio_id,
        signal_date=date(2026, 9, 25),
        effective_after=date(2026, 9, 26),
        ticker="OLD",
        side="Venta",
        target_value_eur=50,
        reason="Sin apertura nueva",
        engine_version=state.engine_version,
    )
    bars = {
        "OLD": pd.DataFrame(
            {"open": [100]},
            index=pd.to_datetime(["2026-09-28"]),
        )
    }

    advanced, trades = fill_pending_orders(
        state,
        [order],
        bars,
        as_of="2026-09-29",
    )

    assert trades == ()
    assert order.id not in advanced.filled_order_ids


def test_effective_after_also_limits_the_first_eligible_open() -> None:
    state = _seed()
    order = PaperOrder(
        id="delayed-sale",
        portfolio_id=state.portfolio_id,
        signal_date=date(2026, 9, 25),
        effective_after=date(2026, 9, 29),
        ticker="OLD",
        side="Venta",
        target_value_eur=50,
        reason="Activación diferida",
        engine_version=state.engine_version,
    )
    bars = {
        "OLD": pd.DataFrame(
            {"open": [100, 101]},
            index=pd.to_datetime(["2026-09-28", "2026-09-29"]),
        )
    }

    _, trades = fill_pending_orders(state, [order], bars, as_of="2026-09-29")

    assert len(trades) == 1
    assert trades[0].filled_at == date(2026, 9, 29)
    assert trades[0].price == 101


def test_duplicate_order_id_is_executed_only_once_per_call() -> None:
    state = _seed()
    order = PaperOrder(
        id="one-sale",
        portfolio_id=state.portfolio_id,
        signal_date=date(2026, 9, 25),
        effective_after=date(2026, 9, 26),
        ticker="OLD",
        side="Venta",
        target_value_eur=50,
        reason="Una sola ejecución",
        engine_version=state.engine_version,
    )

    advanced, trades = fill_pending_orders(
        state,
        [order, order],
        _bars(OLD=100),
        as_of="2026-09-28",
    )

    assert len(trades) == 1
    assert advanced.filled_order_ids == (order.id,)
    assert advanced.costs_cumulative_eur == pytest.approx(trades[0].total_cost_eur)


def test_distinct_orders_cannot_reuse_the_same_id() -> None:
    state = _seed()
    sale = PaperOrder(
        id="collision",
        portfolio_id=state.portfolio_id,
        signal_date=date(2026, 9, 25),
        effective_after=date(2026, 9, 26),
        ticker="OLD",
        side="Venta",
        target_value_eur=50,
        reason="Primera",
        engine_version=state.engine_version,
    )
    conflicting = replace(sale, ticker="KEEP", reason="Distinta")

    with pytest.raises(ValueError, match="identifica órdenes distintas"):
        fill_pending_orders(
            state,
            [sale, conflicting],
            _bars(OLD=100, KEEP=100),
            as_of="2026-09-28",
        )


def test_persisted_currency_mismatch_cannot_reinterpret_native_bars_as_eur() -> None:
    state = _seed()
    order = PaperOrder(
        id="legacy-eur-assumption",
        portfolio_id=state.portfolio_id,
        signal_date=date(2026, 9, 25),
        effective_after=date(2026, 9, 26),
        ticker="OLD",
        side="Venta",
        target_value_eur=50,
        reason="Moneda legacy incorrecta",
        engine_version=state.engine_version,
        currency="EUR",
    )
    bars = _bars(OLD=100)
    bars["OLD"].attrs["currency"] = "USD"

    advanced, trades = fill_pending_orders(
        state,
        [order],
        bars,
        fx_rates_to_eur={"USD": 0.85},
        as_of="2026-09-28",
    )

    assert trades == ()
    assert order.id not in advanced.filled_order_ids


def test_fill_records_each_cost_and_is_idempotent() -> None:
    state = _seed()
    orders = propose_paper_orders(
        _dashboard(), state, as_of="2026-09-25", include_challenger=False
    )
    assumptions = PaperAssumptions(
        buy_fee_eur=1,
        sell_fee_eur=1,
        spread_pct=0.2,
        slippage_pct=0.1,
    )
    bars = _bars(OLD=100, BLUE=50)

    filled, trades = fill_pending_orders(
        state,
        orders,
        bars,
        assumptions=assumptions,
        as_of="2026-09-28",
    )
    repeated, repeated_trades = fill_pending_orders(
        filled,
        orders,
        bars,
        assumptions=assumptions,
        as_of="2026-09-28",
    )

    assert len(trades) == 2
    assert all(trade.fee_eur == 1 for trade in trades)
    assert all(trade.spread_eur == pytest.approx(trade.gross_eur * 0.002) for trade in trades)
    assert all(trade.slippage_eur == pytest.approx(trade.gross_eur * 0.001) for trade in trades)
    assert filled.costs_cumulative_eur == pytest.approx(
        sum(trade.total_cost_eur for trade in trades)
    )
    assert repeated_trades == ()
    assert repeated == filled


def test_challenger_orders_do_not_execute_when_strict_scenario_is_selected() -> None:
    state = _seed()
    orders = propose_paper_orders(_dashboard(), state, as_of="2026-09-25")

    filled, trades = fill_pending_orders(
        state,
        orders,
        _bars(OLD=100, BLUE=50, WATCH=25),
        as_of="2026-09-28",
        scenario="strict",
    )

    assert trades
    assert all(trade.scenario == "strict" for trade in trades)
    assert "WATCH" not in {position.ticker for position in filled.positions}


def test_sale_is_capped_at_available_position_and_never_creates_negative_quantity() -> None:
    state = _seed()
    oversized = PaperOrder(
        id="oversized",
        portfolio_id=state.portfolio_id,
        signal_date=date(2026, 9, 25),
        effective_after=date(2026, 9, 26),
        ticker="OLD",
        side="Venta",
        target_value_eur=1_000_000,
        reason="Prueba de límite",
        engine_version=state.engine_version,
    )

    filled, trades = fill_pending_orders(
        state,
        [oversized],
        _bars(OLD=100),
        as_of="2026-09-28",
    )

    assert trades[0].gross_eur <= state.initial_nav_eur * 0.05
    assert 0 < trades[0].quantity <= 10
    assert next(position for position in filled.positions if position.ticker == "OLD").quantity >= 0
    assert filled.cash_eur >= 0


def test_mark_to_market_reserves_tax_only_on_positive_realized_profit() -> None:
    assumptions = PaperAssumptions(tax_reserve_rate_pct=20)
    profitable = replace(_seed(), realized_pnl_ytd_eur=200)
    losing = replace(_seed(), realized_pnl_ytd_eur=-200)

    profit_snapshot = mark_to_market(
        profitable,
        {"OLD": 110, "KEEP": 90},
        550,
        assumptions,
        as_of="2026-09-29",
    )
    loss_snapshot = mark_to_market(
        losing,
        {"OLD": 110, "KEEP": 90},
        550,
        assumptions,
        as_of="2026-09-29",
    )

    assert profit_snapshot.tax_reserve_eur == 40
    assert loss_snapshot.tax_reserve_eur == 0
    assert profit_snapshot.nav_net_eur == pytest.approx(
        profit_snapshot.nav_gross_eur
        - profit_snapshot.tax_reserve_eur
    )
    assert profit_snapshot.buy_hold_nav_eur == 1_650
    assert profit_snapshot.benchmark_nav_eur == 1_760
    assert profit_snapshot.data_coverage_pct == 100


def test_realized_ytd_is_reset_before_the_first_trade_of_a_new_year() -> None:
    state = replace(_seed(), realized_pnl_ytd_eur=200, realized_pnl_year=2026)
    order = PaperOrder(
        id="next-year-sale",
        portfolio_id=state.portfolio_id,
        signal_date=date(2026, 12, 31),
        effective_after=date(2027, 1, 1),
        ticker="OLD",
        side="Venta",
        target_value_eur=50,
        reason="Prueba cambio fiscal",
        engine_version=state.engine_version,
    )
    bars = {
        "OLD": pd.DataFrame(
            {"open": [100]},
            index=pd.to_datetime(["2027-01-04"]),
        )
    }

    filled, trades = fill_pending_orders(
        state,
        [order],
        bars,
        as_of="2027-01-04",
    )

    assert trades
    assert filled.realized_pnl_year == 2027
    assert filled.realized_pnl_ytd_eur == pytest.approx(trades[0].realized_pnl_eur)
    assert filled.prior_year_tax_reserve_eur == pytest.approx(40)


def test_new_year_keeps_the_prior_tax_liability_out_of_net_nav() -> None:
    assumptions = PaperAssumptions(tax_reserve_rate_pct=20)
    december = replace(
        _seed(),
        realized_pnl_ytd_eur=200,
        realized_pnl_year=2026,
    )
    before = mark_to_market(
        december,
        {"OLD": 100, "KEEP": 100},
        500,
        assumptions,
        as_of="2026-12-31",
    )

    january, trades = fill_pending_orders(
        december,
        [],
        {},
        assumptions=assumptions,
        as_of="2027-01-04",
    )
    after = mark_to_market(
        january,
        {"OLD": 100, "KEEP": 100},
        500,
        assumptions,
        as_of="2027-01-04",
    )

    assert trades == ()
    assert january.realized_pnl_ytd_eur == 0
    assert january.prior_year_tax_reserve_eur == 40
    assert after.tax_reserve_eur == 40
    assert after.nav_net_eur == before.nav_net_eur


def _snapshot(
    day: int,
    *,
    strategy: float,
    hold: float,
    benchmark: float,
) -> PaperSnapshot:
    return PaperSnapshot(
        as_of=date(2026, 9, day),
        nav_gross_eur=strategy,
        nav_net_eur=strategy,
        cash_eur=0,
        holdings_eur=strategy,
        realized_pnl_ytd_eur=0,
        unrealized_pnl_eur=0,
        tax_reserve_eur=0,
        liquidation_cost_eur=0,
        costs_cumulative_eur=5,
        benchmark_nav_eur=benchmark,
        buy_hold_nav_eur=hold,
        data_coverage_pct=100,
    )


def test_scorecard_compares_net_strategy_with_hold_and_benchmark() -> None:
    result = compute_paper_scorecard(
        [
            _snapshot(25, strategy=100, hold=100, benchmark=100),
            _snapshot(26, strategy=90, hold=105, benchmark=103),
            _snapshot(27, strategy=120, hold=110, benchmark=108),
        ]
    )

    assert result["strategy_return_pct"] == pytest.approx(20)
    assert result["buy_hold_return_pct"] == pytest.approx(10)
    assert result["benchmark_return_pct"] == pytest.approx(8)
    assert result["excess_vs_hold_pct"] == pytest.approx(10)
    assert result["excess_vs_benchmark_pct"] == pytest.approx(12)
    assert result["maximum_drawdown_pct"] == pytest.approx(-10)
    assert result["status"] == "Mejora ambas referencias"
