"""Contratos de reconstrucción del laboratorio paper definido en ``app.py``.

Las helpers puras se extraen por AST para no arrancar Streamlit ni escribir en
el diario durante estos tests.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, fields, replace
from pathlib import Path
from hashlib import sha256
import json
from math import isfinite

import pandas as pd
import pytest

from src.data_loader import resolve_analysis_ticker
from src.paper_simulation import (
    PAPER_ENGINE_VERSION,
    PaperAssumptions,
    PaperOrder,
    PaperPosition,
    PaperSnapshot,
    PaperState,
    PaperTrade,
    seed_paper_portfolio,
)


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
HELPER_NAMES = {
    "_rotation_excluded_asset_mask",
    "_rotation_eligible_snapshot",
    "_paper_tracking_tickers",
    "_paper_json_value",
    "_paper_orders_with_known_market_metadata",
    "_paper_market_date",
    "_paper_seed_identifier",
    "_paper_seed_payload",
    "_paper_seed_diagnostics",
    "_paper_assumptions_from_row",
    "_paper_position_from_mapping",
    "_paper_order_from_mapping",
    "_paper_trade_from_mapping",
    "_paper_state_from_history",
    "_paper_snapshots_from_runs",
    "_paper_pending_orders",
}


def _load_helpers() -> dict[str, object]:
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "_ROTATION_EXCLUDED_ASSET_TERMS"
                for target in node.targets
            )
        )
        or (isinstance(node, ast.FunctionDef) and node.name in HELPER_NAMES)
    ]
    namespace: dict[str, object] = {
        "asdict": asdict,
        "fields": fields,
        "replace": replace,
        "isfinite": isfinite,
        "json": json,
        "sha256": sha256,
        "pd": pd,
        "JournalStorageError": RuntimeError,
        "resolve_analysis_ticker": resolve_analysis_ticker,
        "PAPER_ENGINE_VERSION": PAPER_ENGINE_VERSION,
        "PaperAssumptions": PaperAssumptions,
        "PaperOrder": PaperOrder,
        "PaperPosition": PaperPosition,
        "PaperSnapshot": PaperSnapshot,
        "PaperState": PaperState,
        "PaperTrade": PaperTrade,
        "seed_paper_portfolio": seed_paper_portfolio,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), APP_PATH, "exec"), namespace)
    return namespace


HELPERS = _load_helpers()


class _PaperTrackingJournal:
    def __init__(self, simulation: dict[str, object], runs: pd.DataFrame) -> None:
        self._simulation = simulation
        self._runs = runs

    def list_paper_simulations(self, status: str | None = None) -> pd.DataFrame:
        assert status == "active"
        return pd.DataFrame([self._simulation])

    def list_paper_daily_runs(self, simulation_id: int) -> pd.DataFrame:
        assert simulation_id == int(self._simulation["id"])
        return self._runs.copy()


def _paper_order(
    order_id: str,
    ticker: str,
    side: str,
    pair_id: str,
    *,
    scenario: str = "strict",
) -> PaperOrder:
    return PaperOrder(
        id=order_id,
        portfolio_id="7",
        signal_date=pd.Timestamp("2026-09-25").date(),
        effective_after=pd.Timestamp("2026-09-26").date(),
        ticker=ticker,
        side=side,
        target_value_eur=50,
        reason="Prueba",
        scenario=scenario,
        pair_id=pair_id,
    )


def _native_bars(currency: str, rate_to_eur: float) -> pd.DataFrame:
    frame = pd.DataFrame(
        {"open": [100.0]},
        index=pd.to_datetime(["2026-09-28"]),
    )
    frame.attrs["currency"] = currency
    frame.attrs["rate_to_eur"] = rate_to_eur
    return frame


def test_paper_tracking_keeps_initial_current_and_unfilled_order_tickers() -> None:
    pending = _paper_order("pending", "PENDING", "Compra", "pair-pending")
    filled = _paper_order("filled", "ALREADY_FILLED", "Compra", "pair-filled")
    simulation = {
        "id": 7,
        "start_date": "2026-09-25",
        "initial_positions_json": json.dumps([{"ticker": "SOLD_INITIAL"}]),
    }
    runs = pd.DataFrame(
        [
            {
                "id": 1,
                "market_date": "2026-09-26",
                "positions_after_json": json.dumps([{"ticker": "CURRENT_ONLY"}]),
                "proposed_actions_json": json.dumps(
                    [asdict(pending), asdict(filled)], default=str
                ),
                "executed_actions_json": json.dumps(
                    [{"order_id": filled.id}]
                ),
            }
        ]
    )

    tickers = HELPERS["_paper_tracking_tickers"](
        _PaperTrackingJournal(simulation, runs)
    )

    assert tickers == ["SOLD_INITIAL", "CURRENT_ONLY", "PENDING"]


def test_market_metadata_filter_is_atomic_for_each_order_pair() -> None:
    strict = (
        _paper_order("strict-sell", "OLD", "Venta", "strict-pair"),
        _paper_order("strict-buy", "USD_ASSET", "Compra", "strict-pair"),
    )
    challenger = (
        _paper_order(
            "challenger-sell",
            "OLD",
            "Venta",
            "challenger-pair",
            scenario="challenger",
        ),
        _paper_order(
            "challenger-buy",
            "WATCH",
            "Compra",
            "challenger-pair",
            scenario="challenger",
        ),
    )
    bars = {
        "OLD": _native_bars("EUR", 1.0),
        "WATCH": _native_bars("EUR", 1.0),
    }

    filtered = HELPERS["_paper_orders_with_known_market_metadata"](
        (*strict, *challenger),
        bars,
    )

    assert filtered == challenger
    assert all(order.currency == "EUR" for order in filtered)


def test_market_metadata_filter_persists_verified_native_currency_and_fx() -> None:
    orders = (
        _paper_order("sell", "OLD", "Venta", "pair"),
        _paper_order("buy", "USD_ASSET", "Compra", "pair"),
    )
    bars = {
        "OLD": _native_bars("EUR", 1.0),
        "USD_ASSET": _native_bars("USD", 0.85),
    }

    filtered = HELPERS["_paper_orders_with_known_market_metadata"](orders, bars)

    assert [(order.ticker, order.currency) for order in filtered] == [
        ("OLD", "EUR"),
        ("USD_ASSET", "USD"),
    ]


def test_market_metadata_filter_rejects_pair_without_reliable_fx() -> None:
    orders = (
        _paper_order("sell", "OLD", "Venta", "pair"),
        _paper_order("buy", "USD_ASSET", "Compra", "pair"),
    )
    invalid_usd = _native_bars("USD", 0.85)
    invalid_usd.attrs.pop("rate_to_eur")

    filtered = HELPERS["_paper_orders_with_known_market_metadata"](
        orders,
        {"OLD": _native_bars("EUR", 1.0), "USD_ASSET": invalid_usd},
    )

    assert filtered == ()


def test_seed_payload_aggregates_listed_positions_and_keeps_cash_separate() -> None:
    snapshot = pd.DataFrame(
        [
            {
                "platform": "Broker",
                "asset_name": "Alpha lot 1",
                "asset_type": "Acción",
                "analysis_ticker": "AAA",
                "quantity": 2,
                "value_eur": 200,
                "cost_estimate_eur": 180,
            },
            {
                "platform": "Broker",
                "asset_name": "Alpha lot 2",
                "asset_type": "Acción",
                "analysis_ticker": "AAA",
                "quantity": 1,
                "value_eur": 100,
                "cost_estimate_eur": 90,
            },
            {
                "platform": "Broker",
                "asset_name": "Efectivo",
                "asset_type": "Efectivo",
                "analysis_ticker": "",
                "quantity": None,
                "value_eur": 50,
            },
            {
                "platform": "Civislend",
                "asset_name": "Proyecto",
                "asset_type": "Crowdlending",
                "analysis_ticker": "PRIVATE",
                "quantity": 1,
                "value_eur": 1_000,
            },
        ]
    )

    payload, cash = HELPERS["_paper_seed_payload"](
        snapshot,
        {"AAA": 100},
        [{"Ticker": "AAA", "Sector": "Technology"}],
    )

    assert cash == 50
    assert payload == [
        {
            "ticker": "AAA",
            "quantity": 3.0,
            "price_eur": 100,
            "value_eur": 300.0,
            "cost_basis_eur": 270.0,
            "sector": "Technology",
            "currency": "EUR",
            "valuation_mode": "market",
            "source_accounts": "Broker",
            "display_name": "Alpha lot 1 / Alpha lot 2",
        }
    ]


def test_seed_payload_fills_only_the_unknown_lot_cost_with_market_value() -> None:
    snapshot = pd.DataFrame(
        [
            {
                "asset_name": "Alpha known",
                "asset_type": "Acción",
                "analysis_ticker": "AAA",
                "quantity": 1,
                "value_eur": 100,
                "cost_estimate_eur": 80,
            },
            {
                "asset_name": "Alpha unknown",
                "asset_type": "Acción",
                "analysis_ticker": "AAA",
                "quantity": 1,
                "value_eur": 120,
                "cost_estimate_eur": None,
            },
        ]
    )

    payload, _ = HELPERS["_paper_seed_payload"](snapshot, {"AAA": 110}, [])

    assert payload[0]["cost_basis_eur"] == 200


def test_seed_diagnostic_makes_excluded_ticker_and_value_coverage_visible() -> None:
    snapshot = pd.DataFrame(
        [
            {
                "asset_name": "Alpha",
                "asset_type": "Acción",
                "analysis_ticker": "AAA",
                "value_eur": 300,
            },
            {
                "asset_name": "Beta",
                "asset_type": "Acción",
                "analysis_ticker": "BBB",
                "value_eur": 100,
            },
        ]
    )
    payload = [{"ticker": "AAA", "value_eur": 300}]

    result = HELPERS["_paper_seed_diagnostics"](snapshot, payload)

    assert result["missing"] == ["BBB"]
    assert result["coverage_pct"] == 75


def test_seed_includes_both_banks_and_manual_holdings_without_a_ticker() -> None:
    snapshot = pd.DataFrame(
        [
            {
                "platform": "Revolut",
                "asset_name": "AST SpaceMobile",
                "asset_type": "Acción",
                "analysis_ticker": "ASTS",
                "quantity": 2,
                "value_eur": 200,
                "cost_estimate_eur": 180,
            },
            {
                "platform": "Trade Republic",
                "asset_name": "American Express",
                "asset_type": "Acción",
                "analysis_ticker": "AXP",
                "quantity": None,
                "value_eur": 800,
                "cost_estimate_eur": 750,
            },
            {
                "platform": "Trade Republic",
                "asset_name": "MSCI Emerging Markets ex China",
                "asset_type": "ETF",
                "analysis_ticker": "",
                "quantity": None,
                "value_eur": 400,
                "cost_estimate_eur": 390,
            },
            {
                "platform": "Revolut",
                "asset_name": "Efectivo",
                "asset_type": "Efectivo",
                "analysis_ticker": "",
                "value_eur": 1.22,
            },
        ]
    )

    payload, cash = HELPERS["_paper_seed_payload"](
        snapshot,
        {"ASTS": 100},
        [],
    )
    diagnostic = HELPERS["_paper_seed_diagnostics"](snapshot, payload)

    assert cash == 1.22
    assert len(payload) == 3
    assert {row["source_accounts"] for row in payload} == {
        "Revolut",
        "Trade Republic",
    }
    frozen = [row for row in payload if row["valuation_mode"] == "frozen"]
    assert len(frozen) == 2
    assert any(str(row["ticker"]).startswith("MANUAL_") for row in frozen)
    assert sum(float(row["value_eur"]) for row in payload) + cash == 1_401.22
    assert diagnostic["missing"] == []
    assert diagnostic["included_coverage_pct"] == 100
    assert diagnostic["market_coverage_pct"] == pytest.approx(200 / 1_400 * 100)
    assert {row["platform"] for row in diagnostic["accounts"]} == {
        "Revolut",
        "Trade Republic",
    }


def test_persisted_state_round_trip_restores_costs_tax_year_and_filled_orders() -> None:
    assumptions = {
        **asdict(PaperAssumptions(max_daily_turnover_pct=4)),
        "benchmark_initial_price_eur": 500,
    }
    initial_positions = [
        {
            "ticker": "AAA",
            "quantity": 2,
            "price_eur": 100,
            "value_eur": 200,
            "cost_basis_eur": 180,
            "sector": "Technology",
            "currency": "EUR",
        }
    ]
    simulation = {
        "id": 7,
        "start_date": "2026-09-25",
        "initial_cash_eur": 20,
        "initial_positions_json": json.dumps(initial_positions),
        "assumptions_json": json.dumps(assumptions),
        "engine_version": PAPER_ENGINE_VERSION,
    }
    latest_position = PaperPosition("AAA", 1.5, 90, 110, "Technology")
    trade = PaperTrade(
        ticker="AAA",
        side="Venta",
        quantity=0.5,
        price=110,
        currency="EUR",
        fx_rate_to_eur=1,
        gross_eur=55,
        fee_eur=1,
        spread_eur=0.08,
        slippage_eur=0.06,
        fx_cost_eur=0,
        net_cash_eur=53.86,
        realized_pnl_eur=8.86,
        filled_at=pd.Timestamp("2026-09-27").date(),
        order_id="filled-2",
        scenario="strict",
        pair_id="pair-1",
    )
    runs = pd.DataFrame(
        [
            {
                "id": 1,
                "market_date": "2026-09-26",
                "positions_after_json": json.dumps([asdict(latest_position)]),
                "executed_actions_json": json.dumps([{"order_id": "filled-1"}]),
                "cash_eur": 70,
                "realized_pnl_eur": 9,
                "cumulative_costs_eur": 2,
            },
            {
                "id": 2,
                "market_date": "2026-09-27",
                "positions_after_json": json.dumps([asdict(latest_position)]),
                "executed_actions_json": json.dumps([asdict(trade)], default=str),
                "cash_eur": 70,
                "realized_pnl_eur": 9,
                "cumulative_costs_eur": 2,
            },
        ]
    )

    state = HELPERS["_paper_state_from_history"](simulation, runs)

    assert state.portfolio_id == "7"
    assert state.as_of.isoformat() == "2026-09-27"
    assert state.positions == (latest_position,)
    assert state.initial_nav_eur == 220
    assert state.benchmark_initial_price_eur == 500
    assert state.realized_pnl_ytd_eur == 9
    assert state.realized_pnl_year == 2026
    assert state.costs_cumulative_eur == 2
    assert state.filled_order_ids == ("filled-1", "filled-2")
    assert state.trades == (trade,)


def test_pending_order_parser_ignores_invalid_rows_without_breaking_history() -> None:
    valid = PaperOrder(
        id="order-1",
        portfolio_id="7",
        signal_date=pd.Timestamp("2026-09-25").date(),
        effective_after=pd.Timestamp("2026-09-26").date(),
        ticker="AAA",
        side="Compra",
        target_value_eur=50,
        reason="Prueba",
    )
    runs = pd.DataFrame(
        [
            {
                "market_date": "2026-09-25",
                "id": 1,
                "proposed_actions_json": json.dumps(
                    [asdict(valid), {"id": "incompleta"}], default=str
                ),
            }
        ]
    )

    parsed = HELPERS["_paper_pending_orders"](runs)

    assert parsed == (valid,)


def test_snapshots_rebuilt_from_runs_keep_the_three_comparable_nav_series() -> None:
    runs = pd.DataFrame(
        [
            {
                "id": 1,
                "market_date": "2026-09-25",
                "net_nav_eur": 990,
                "cash_eur": 100,
                "tax_reserve_eur": 10,
                "realized_pnl_eur": 50,
                "unrealized_pnl_eur": -5,
                "cumulative_costs_eur": 2,
                "benchmark_nav_eur": 1_010,
                "hold_nav_eur": 1_005,
                "coverage_pct": 95,
            }
        ]
    )

    snapshots = HELPERS["_paper_snapshots_from_runs"](runs)

    assert len(snapshots) == 1
    assert snapshots[0].nav_gross_eur == 1_000
    assert snapshots[0].nav_net_eur == 990
    assert snapshots[0].holdings_eur == 900
    assert snapshots[0].buy_hold_nav_eur == 1_005
    assert snapshots[0].benchmark_nav_eur == 1_010
    assert snapshots[0].data_coverage_pct == 95


def test_market_date_uses_latest_available_session_not_wall_clock() -> None:
    prepared = {
        "AAA": pd.DataFrame(
            {"close": [1, 2]},
            index=pd.to_datetime(["2026-09-24", "2026-09-25"]),
        ),
        "BBB": pd.DataFrame(
            {"close": [3]},
            index=pd.to_datetime(["2026-09-26"]),
        ),
    }

    assert HELPERS["_paper_market_date"](prepared).isoformat() == "2026-09-26"


def test_market_date_prefers_spy_session_over_other_exchange_calendar() -> None:
    prepared = {
        "JP": pd.DataFrame(
            {"close": [1]},
            index=pd.to_datetime(["2026-09-28"]),
        )
    }
    reference = {
        "SPY": pd.DataFrame(
            {"close": [500]},
            index=pd.to_datetime(["2026-09-25"]),
        )
    }

    assert HELPERS["_paper_market_date"](prepared, reference).isoformat() == "2026-09-25"
