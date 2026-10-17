"""Synthetic, storage-free accounting tests. No personal workbooks as fixtures."""

from copy import deepcopy

import pandas as pd
from pandas.testing import assert_frame_equal
import pytest

from src.portfolio_reconciliation import build_reconciled_portfolio


COMPLETE = {
    "operations_complete": True, "capital_flows_complete": True,
    "income_complete": True, "costs_complete": True,
}


def holding(ticker="AAA", quantity=10, cost=100, value=120, platform="Broker", currency="EUR", **extra):
    return {"snapshot_date": "2026-10-13", "platform": platform, "asset_name": ticker,
            "analysis_ticker": ticker, "currency": currency, "quantity": quantity,
            "cost_estimate_eur": cost, "value_eur": value, "asset_type": "Acción", **extra}


def cash(amount=500, platform="Broker", currency="EUR"):
    return holding("", None, amount, amount, platform, currency, asset_name="Efectivo", asset_type="Efectivo")


def trade(side="Compra", quantity=10, price=10, day="2026-10-01", ticker="AAA", platform="Broker", currency="EUR", **extra):
    return {"executed_at": day, "platform": platform, "account_name": platform,
            "ticker": ticker, "currency": currency, "side": side, "quantity": quantity,
            "price": price, "fees": 0.0, "fx_cost_eur": 0.0, **extra}


def security(report, ticker="AAA", platform="Broker", currency="EUR"):
    return report.positions.loc[(report.positions["ticker"] == ticker) & (report.positions["platform"] == platform) & (report.positions["currency"] == currency)].iloc[0]


def test_snapshot_is_opening_balance_not_a_second_purchase():
    report = build_reconciled_portfolio(pd.DataFrame([holding(), cash()]), pd.DataFrame([trade()]))
    row = security(report)
    assert row["quantity"] == 10
    assert row["cost_basis_eur"] == 100
    assert report.metrics_global["value_eur"] == 620
    assert report.metrics_global["net_contributions_eur"] is None
    assert report.metrics_global["realized_pnl_eur"] is None
    assert report.metrics_global["total_return_pct"] is None


def test_eur_executions_join_one_usd_quoted_snapshot_position_without_duplicate_inventory():
    snapshot = pd.DataFrame([holding(currency="USD"), cash(100)])
    operations = pd.DataFrame([trade(quantity=10, price=10), trade(quantity=2, price=20, day="2026-10-15")])
    report = build_reconciled_portfolio(snapshot, operations,
        prices={"AAA": {"price": 24, "currency": "USD", "fx_rate_to_eur": 1 / 1.2, "as_of": "2026-10-15"}})
    securities = report.positions.loc[report.positions.ticker == "AAA"]
    assert len(securities) == 1
    assert securities.iloc[0].currency == "USD"
    assert securities.iloc[0].quantity == 12
    assert securities.iloc[0].cost_basis_eur == 140
    assert securities.iloc[0].value_eur == pytest.approx(240)
    assert report.metrics_global["cash_value_eur"] == 60
    assert report.metrics_global["value_eur"] == pytest.approx(300)


def test_cross_quote_currency_inventory_preserves_native_historical_fifo_reporting():
    snapshot = pd.DataFrame([holding(quantity=2, cost=20, value=24, currency="USD"), cash(100)])
    operations = pd.DataFrame([
        trade(quantity=3, price=10, day="2026-10-01"),
        trade("Venta", quantity=1, price=15, day="2026-10-02"),
        trade("Venta", quantity=1, price=20, day="2026-10-15"),
    ])
    report = build_reconciled_portfolio(snapshot, operations, coverage=COMPLETE)
    assert len(report.positions.loc[report.positions.ticker == "AAA"]) == 1
    assert security(report, currency="USD").quantity == 1
    assert report.realized_trades.currency.tolist() == ["EUR", "EUR"]
    assert report.realized_trades.inventory_currency.tolist() == ["USD", "USD"]
    assert report.realized_trades.realized_pnl_local.tolist() == [5, 10]
    assert report.realized_trades.realized_pnl_eur.tolist() == [5, 10]
    assert security(report, currency="USD").realized_pnl_eur == 15
    assert security(report, currency="USD").cost_basis_local_currency == "EUR"


def test_mixed_execution_currency_lots_do_not_sum_incompatible_native_costs():
    snapshot = pd.DataFrame([holding(quantity=1, cost=10, value=12, currency="USD",
                                    cost_basis_local=12), cash(100)])
    operations = pd.DataFrame([trade(quantity=1, price=20, day="2026-10-15")])
    report = build_reconciled_portfolio(snapshot, operations)
    row = security(report, currency="USD")
    assert row.quantity == 2
    assert row.cost_basis_eur == 30
    assert pd.isna(row.cost_basis_local)
    assert row.cost_basis_local_currency == ""


def test_source_statement_order_survives_reverse_database_reads_for_same_day_fifo():
    rows = [
        trade(quantity=1, price=10, day="2026-10-01", source_row=2, source_sheet="Synthetic operations", source_event_id="cheap"),
        trade(quantity=1, price=20, day="2026-10-01", source_row=3, source_sheet="Synthetic operations", source_event_id="expensive"),
        trade("Venta", quantity=1, price=30, day="2026-10-02", source_row=4, source_sheet="Synthetic operations", source_event_id="sale"),
    ]
    report = build_reconciled_portfolio(pd.DataFrame([holding(quantity=1, cost=20), cash()]), pd.DataFrame(list(reversed(rows))))
    assert report.realized_trades.iloc[0].realized_pnl_eur == 20
    assert not any("inventario suficiente" in issue for issue in report.issues)


def test_same_day_documented_buy_before_sale_is_not_reversed_by_database_id_order():
    rows = [
        trade(quantity=2, price=10, day="2026-10-01", source_row=2, source_sheet="Synthetic operations", source_event_id="buy"),
        trade("Venta", quantity=1, price=15, day="2026-10-01", source_row=3, source_sheet="Synthetic operations", source_event_id="sale"),
    ]
    report = build_reconciled_portfolio(pd.DataFrame([holding(quantity=1, cost=10), cash()]), pd.DataFrame(list(reversed(rows))))
    assert report.realized_trades.iloc[0].realized_pnl_eur == 5
    assert not any("inventario suficiente" in issue for issue in report.issues)


def test_post_cutoff_sale_then_next_day_buy_preserves_fifo_and_cash():
    snapshot = pd.DataFrame([holding(quantity=10, cost=120, value=140), cash(100)])
    operations = pd.DataFrame([
        trade(quantity=4, price=10), trade(quantity=6, price=80 / 6, day="2026-10-02"),
        trade("Venta", 4, 15, "2026-10-14"), trade("Compra", 2, 12, "2026-10-15"),
    ])
    report = build_reconciled_portfolio(snapshot, operations, coverage=COMPLETE)
    row = security(report)
    assert row["quantity"] == 8
    assert row["cost_basis_eur"] == pytest.approx(104)
    assert row["realized_pnl_eur"] == pytest.approx(20)
    assert report.metrics_global["cash_value_eur"] == 136
    assert row["value_eur"] == pytest.approx(112)
    assert "histórico reconciliado" in row["basis_quality"]


def test_same_day_included_only_when_explicitly_after_snapshot():
    snapshot = pd.DataFrame([holding(), cash()])
    ignored = trade(quantity=2, day="2026-10-13")
    confirmed = trade(quantity=3, day="2026-10-13", after_snapshot=True)
    next_day = trade(quantity=4, day="2026-10-14")
    report = build_reconciled_portfolio(snapshot, pd.DataFrame([ignored, confirmed, next_day]))
    assert security(report)["quantity"] == 17
    assert report.metrics_global["cash_value_eur"] == 430


def test_explicit_historical_phase_does_not_change_snapshot_even_if_date_is_newer():
    report = build_reconciled_portfolio(pd.DataFrame([holding(), cash()]), pd.DataFrame([trade(quantity=2, day="2026-10-14", phase="historical")]))
    assert security(report)["quantity"] == 10


def test_accounts_and_currencies_never_share_fifo_lots():
    operations = pd.DataFrame([
        trade(quantity=1, price=10, platform="A"), trade(quantity=1, price=20, platform="B"),
        trade(quantity=1, price=30, platform="A", currency="USD", settlement_amount_local=30),
        trade("Venta", 1, 25, "2026-10-02", platform="B"),
    ])
    report = build_reconciled_portfolio(pd.DataFrame(), operations)
    assert security(report, platform="A")["cost_basis_eur"] == 10
    assert security(report, platform="A", currency="USD")["cost_basis_local"] == 30
    assert report.realized_trades.iloc[0]["realized_pnl_eur"] == 5
    assert not ((report.positions.platform == "B") & (report.positions.ticker == "AAA")).any()


def test_snapshot_closure_does_not_resurrect_old_ledger_position():
    report = build_reconciled_portfolio(pd.DataFrame([holding("BBB"), cash()]), pd.DataFrame([trade(ticker="AAA")]))
    assert set(report.positions.ticker) == {"BBB", ""}


def test_partial_sale_of_unverified_snapshot_lot_does_not_invent_average_fifo():
    report = build_reconciled_portfolio(pd.DataFrame([holding(), cash()]), pd.DataFrame([trade("Venta", 2, 15, "2026-10-14")]))
    row = security(report)
    assert row["quantity"] == 8
    assert pd.isna(row["cost_basis_eur"])
    assert pd.isna(row["unrealized_pnl_eur"])
    assert pd.isna(report.realized_trades.iloc[0]["realized_pnl_eur"])


def test_full_sale_can_use_documented_aggregate_basis_without_fifo_guess():
    report = build_reconciled_portfolio(pd.DataFrame([holding(), cash()]), pd.DataFrame([trade("Venta", 10, 15, "2026-10-14")]))
    assert report.realized_trades.iloc[0]["realized_pnl_eur"] == 50
    assert not (report.positions.ticker == "AAA").any()
    assert report.metrics_global["realized_pnl_known_eur"] == 50
    assert report.metrics_global["realized_pnl_eur"] is None


def test_complete_assertion_still_requires_quantity_and_cost_reconciliation():
    report = build_reconciled_portfolio(pd.DataFrame([holding(cost=200), cash()]), pd.DataFrame([trade(), trade("Venta", 2, 15, "2026-10-14")]), coverage=COMPLETE)
    assert pd.isna(security(report)["cost_basis_eur"])
    assert any("FIFO histórico no reconciliado" in issue for issue in report.issues)


def test_missing_inventory_is_visible_not_silently_clipped_or_backfilled():
    report = build_reconciled_portfolio(pd.DataFrame([holding(quantity=1, cost=10, value=12), cash()]), pd.DataFrame([trade("Venta", 2, 15, "2026-10-14")]))
    assert pd.isna(security(report)["quantity"])
    assert not security(report)["quantity_complete"]
    assert pd.isna(report.realized_trades.iloc[0]["realized_pnl_eur"])
    assert any("inventario suficiente" in issue for issue in report.issues)


def test_cash_and_illiquid_principal_are_separate_without_expected_interest():
    snapshot = pd.DataFrame([
        holding("", None, 4000, 4000, "Segofactoring", asset_name="Principal prestado", asset_type="Factoring"),
        cash(505, "Segofactoring"),
        holding("", None, 2000, 2000, "Civislend", asset_name="Préstamos activos", asset_type="Crowdlending"),
    ])
    report = build_reconciled_portfolio(snapshot, pd.DataFrame())
    assert report.metrics_global["value_eur"] == 6505
    assert report.metrics_global["illiquid_value_eur"] == 6000
    assert report.metrics_global["cash_value_eur"] == 505
    assert report.metrics_global["interest_eur"] is None
    assert report.metrics_global["interest_known_eur"] == 0


def test_cash_insufficient_for_post_snapshot_buys_is_unknown_not_zero_or_deposit():
    report = build_reconciled_portfolio(pd.DataFrame([holding(), cash(20)]), pd.DataFrame([trade(quantity=3, price=10, day="2026-10-14")]))
    assert report.metrics_global["cash_value_eur"] is None
    assert report.metrics_global["value_eur"] is None
    assert report.metrics_global["net_contributions_eur"] is None
    assert any("Efectivo insuficiente" in issue for issue in report.issues)
    assert report.monthly.iloc[0]["ending_value_eur"] == 140


def test_missing_cash_opening_balance_does_not_claim_complete_wealth():
    report = build_reconciled_portfolio(pd.DataFrame([holding()]), pd.DataFrame([trade(quantity=1, day="2026-10-14")]))
    assert report.metrics_global["value_eur"] is None
    assert report.metrics_global["value_known_eur"] == 132
    assert not report.metrics_by_platform.iloc[0]["value_complete"]


def test_recorded_native_settlement_preserves_fifo_without_reference_fx():
    operations = pd.DataFrame([
        trade(quantity=2, price=50, currency="USD", settlement_amount_local=102, fees=0, fee_quality="unknown", fx_quality="reference", reference_fx_rate_to_eur=0.9),
        trade("Venta", 1, 60, "2026-10-02", currency="USD", settlement_amount_local=59, fees=0, fee_quality="unknown", fx_quality="reference", reference_fx_rate_to_eur=0.9),
    ])
    report = build_reconciled_portfolio(pd.DataFrame(), operations, coverage=COMPLETE)
    assert security(report, currency="USD")["cost_basis_local"] == 51
    assert pd.isna(security(report, currency="USD")["cost_basis_eur"])
    assert report.realized_trades.iloc[0]["realized_pnl_local"] == 8
    assert pd.isna(report.realized_trades.iloc[0]["realized_pnl_eur"])
    assert report.metrics_global["fees_eur"] is None
    assert report.metrics_global["total_pnl_eur"] is None


def test_exact_recorded_fx_not_a_present_day_rate_can_value_transactions():
    operations = pd.DataFrame([trade(quantity=2, price=50, currency="USD", fees=2, fx_rate_to_eur=0.9), trade("Venta", 1, 60, "2026-10-02", currency="USD", fees=1, fx_rate_to_eur=0.8)])
    report = build_reconciled_portfolio(pd.DataFrame(), operations)
    assert report.realized_trades.iloc[0]["realized_pnl_eur"] == pytest.approx(59 * 0.8 - 51 * 0.9)


def test_isin_identity_reconciles_without_making_it_a_market_ticker():
    isin = "IE00B1234567"
    report = build_reconciled_portfolio(pd.DataFrame([holding("", raw_identifier=isin), cash()]), pd.DataFrame([trade(ticker=isin, quantity=2, day="2026-10-14")]))
    assert len(report.positions) == 2
    assert security(report, isin)["quantity"] == 12
    assert security(report, isin)["analysis_ticker"] == ""


def test_documented_flows_are_not_trade_amounts_or_internal_transfers():
    events = pd.DataFrame([
        {"executed_at": "2026-10-01", "platform": "Broker", "event_type": "contribution", "amount_eur": 1000},
        {"executed_at": "2026-10-02", "platform": "Broker", "event_type": "withdrawal", "amount_eur": 100},
        {"executed_at": "2026-10-03", "platform": "Broker", "event_type": "transfer_in", "amount_eur": 300},
        {"executed_at": "2026-10-04", "platform": "Broker", "event_type": "dividend", "amount_eur": 10},
    ])
    report = build_reconciled_portfolio(pd.DataFrame([holding(), cash()]), pd.DataFrame([trade()]), events, coverage=COMPLETE)
    assert report.metrics_global["contributed_capital_eur"] == 1000
    assert report.metrics_global["withdrawn_capital_eur"] == 100
    assert report.metrics_global["net_contributions_eur"] == 900
    assert report.metrics_global["dividends_eur"] == 10
    assert report.metrics_global["total_return_pct"] is None


def test_unknown_event_amount_does_not_become_zero_component():
    events = pd.DataFrame([{"executed_at": "2026-10-01", "platform": "Broker", "event_type": "interest", "amount": 10, "currency": "USD"}])
    report = build_reconciled_portfolio(pd.DataFrame([holding()]), pd.DataFrame(), events, coverage=COMPLETE)
    assert report.metrics_global["interest_eur"] is None
    assert report.metrics_global["interest_known_eur"] == 0
    assert report.metrics_global["total_pnl_eur"] is None


def test_wealth_history_is_actual_dated_marks_not_cumulative_purchases():
    operations = pd.DataFrame([trade(price=9, day="2026-01-01"), trade("Venta", 10, 11, "2026-02-01")])
    report = build_reconciled_portfolio(pd.DataFrame([holding(), cash()]), operations)
    assert pd.isna(report.monthly.loc[report.monthly.period == "2026-01", "ending_value_eur"].iloc[0])
    october = report.monthly.loc[report.monthly.period == "2026-10"].iloc[0]
    assert october["ending_value_eur"] == 620
    assert october["valuation_date"] == "2026-10-13"
    assert not october["valuation_is_period_end"]
    assert report.annual.iloc[0]["total_return_pct"] is None


def test_idempotence_with_source_keys_preserves_inputs_and_legitimate_identical_fills():
    snapshot = pd.DataFrame([holding(), cash()])
    rows = [trade(quantity=1, day="2026-10-14", source_event_id="fill1"), trade(quantity=1, day="2026-10-14", source_event_id="fill2")]
    operations = pd.DataFrame(rows + deepcopy(rows))
    original_snapshot, original_ops = snapshot.copy(deep=True), operations.copy(deep=True)
    first = build_reconciled_portfolio(snapshot, operations)
    second = build_reconciled_portfolio(snapshot, operations)
    assert security(first)["quantity"] == 12
    assert_frame_equal(first.positions, second.positions)
    assert_frame_equal(snapshot, original_snapshot)
    assert_frame_equal(operations, original_ops)


def test_future_operations_do_not_leak_into_as_of_report():
    report = build_reconciled_portfolio(pd.DataFrame([holding(), cash()]), pd.DataFrame([trade(quantity=2, day="2026-10-14"), trade(quantity=3, day="2026-10-16")]), as_of="2026-10-15")
    assert security(report)["quantity"] == 12


def test_report_with_no_records_does_not_claim_zero_wealth_is_known():
    report = build_reconciled_portfolio(pd.DataFrame(), pd.DataFrame())
    assert report.positions.empty
    assert report.metrics_global["value_eur"] is None
    assert report.metrics_global["total_return_pct"] is None


def test_aggregate_metrics_do_not_deduct_embedded_trade_fees_twice():
    ops = pd.DataFrame([trade(quantity=10, price=10, fees=1), trade("Venta", 10, 12, "2026-10-02", fees=1)])
    events = pd.DataFrame([{"executed_at": "2026-10-03", "platform": "Broker", "event_type": "tax", "amount_eur": 2}])
    report = build_reconciled_portfolio(pd.DataFrame([cash(518)]), ops, events, coverage=COMPLETE)
    assert report.metrics_global["realized_pnl_eur"] == 18
    assert report.metrics_global["fees_eur"] == 2
    assert report.metrics_global["total_pnl_eur"] == 16


def test_value_only_snapshot_remains_valid_without_inventing_quantity():
    report = build_reconciled_portfolio(pd.DataFrame([holding(quantity=None, cost=100, value=120)]), pd.DataFrame())
    row = security(report)
    assert pd.isna(row["quantity"])
    assert row["value_eur"] == 120
    assert row["cost_basis_eur"] == 100
    assert row["unrealized_pnl_eur"] == 20


def test_same_day_documented_deposit_can_fund_buy_without_assuming_intraday_order():
    events = pd.DataFrame([{"executed_at": "2026-10-14", "platform": "Broker", "event_type": "contribution", "amount_eur": 30}])
    report = build_reconciled_portfolio(pd.DataFrame([holding(), cash(0)]), pd.DataFrame([trade(quantity=3, price=10, day="2026-10-14")]), events)
    assert report.metrics_global["cash_value_eur"] == 0
    assert not any("Efectivo insuficiente" in issue for issue in report.issues)
    assert report.metrics_global["contributed_capital_known_eur"] == 30
    assert report.metrics_global["contributed_capital_eur"] is None


def test_missing_fx_for_trading_settlement_invalidates_existing_cash_value():
    report = build_reconciled_portfolio(pd.DataFrame([holding(currency="USD"), cash(500, currency="USD")]), pd.DataFrame([trade(quantity=1, day="2026-10-14", currency="USD", settlement_amount_local=10)]))
    assert report.metrics_global["cash_value_eur"] is None
    assert report.metrics_global["value_eur"] is None
    assert any("Liquidación o cambio no documentado" in issue for issue in report.issues)


def test_quotes_and_valuation_history_after_as_of_are_not_used():
    report = build_reconciled_portfolio(pd.DataFrame([holding()]), pd.DataFrame(),
        prices={"AAA": {"price": 100, "currency": "EUR", "as_of": "2026-10-16"}},
        valuation_history=pd.DataFrame([{"date": "2026-11-01", "value_eur": 1000}]), as_of="2026-10-15")
    assert report.metrics_global["value_eur"] == 120
    assert list(report.monthly.period) == ["2026-10"]
    assert any("Cotización posterior" in issue for issue in report.issues)


def test_latest_snapshot_per_account_is_not_summed_with_previous_snapshots():
    old = holding(value=90, snapshot_date="2026-10-01")
    report = build_reconciled_portfolio(pd.DataFrame([old, holding(value=120)]), pd.DataFrame())
    assert report.metrics_global["value_eur"] == 120
    assert len(report.positions) == 1


def test_unclassified_event_never_becomes_an_external_contribution():
    report = build_reconciled_portfolio(pd.DataFrame([holding()]), pd.DataFrame(), pd.DataFrame([{"executed_at": "2026-10-14", "platform": "Broker", "event_type": "money", "amount_eur": 100}]))
    assert report.cash_flows.empty
    assert report.metrics_global["contributed_capital_known_eur"] == 0
    assert any("Evento no clasificado" in issue for issue in report.issues)
