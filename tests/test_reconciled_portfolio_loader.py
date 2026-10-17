"""Synthetic read-only loader contracts; no private records or network access."""

from copy import deepcopy
import json

import pandas as pd
from pandas.testing import assert_frame_equal
import pytest

import src.reconciled_portfolio_loader as loader


class Journal:
    def __init__(self, metadata=None):
        self.calls = []
        self.batches = pd.DataFrame([{"id": 1, "metadata": metadata or {}, "created_at": "2026-10-15"}])

    def list_portfolio_imports(self, **kwargs):
        self.calls.append(kwargs)
        return self.batches.copy(deep=True)


def holding(ticker="AAA", value=120, quantity=10, cost=100, platform="Broker", currency="EUR", day="2026-10-13"):
    return {"snapshot_date": day, "platform": platform, "asset_name": ticker or "Efectivo",
            "analysis_ticker": ticker, "currency": currency, "quantity": quantity,
            "cost_estimate_eur": cost, "value_eur": value,
            "asset_type": "Acción" if ticker else "Efectivo"}


def snapshot(currency="EUR"):
    return pd.DataFrame([holding(currency=currency), holding("", 500, None, 500)])


def trade(**extra):
    return {"ticker": "AAA", "account_name": "Broker", "side": "Compra", "quantity": 2,
            "price": 10, "fees": 0, "currency": "EUR", "executed_at": "2026-10-15",
            "settlement_amount_eur": 20, "settlement_amount_local": 20,
            "source_event_id": "synthetic-operation-1", "notes": "", **extra}


def event(**extra):
    return {"source_event_id": "synthetic-dividend-1", "event_type": "dividend",
            "executed_at": "2026-10-14", "platform": "Broker", "account_name": "Broker",
            "ticker": "AAA", "currency": "EUR", "amount": 3, "amount_eur": 3, **extra}


def test_no_import_capability_or_empty_batches_returns_none_without_fallback():
    assert loader.load_reconciled_portfolio(object(), snapshot(), pd.DataFrame()) is None
    journal = Journal()
    journal.batches = pd.DataFrame()
    assert loader.load_reconciled_portfolio(journal, snapshot(), pd.DataFrame()) is None
    assert journal.calls == [{}]


def test_read_errors_and_invalid_metadata_propagate_without_live_fallback():
    class Broken:
        def list_portfolio_imports(self):
            raise RuntimeError("Synthetic backend error")
    with pytest.raises(RuntimeError, match="backend"):
        loader.load_reconciled_portfolio(Broken(), snapshot(), pd.DataFrame())
    journal = Journal()
    journal.batches.loc[0, "metadata"] = "not JSON"
    with pytest.raises(ValueError, match="JSON"):
        loader.load_reconciled_portfolio(journal, snapshot(), pd.DataFrame())
    with pytest.raises(ValueError, match="fecha"):
        loader.load_reconciled_portfolio(Journal(), snapshot(), pd.DataFrame(), reference_date="not a date")


def test_raw_inputs_pass_to_accounting_without_pre_reconciliation_or_mutation(monkeypatch):
    snapshots, operations = snapshot(), pd.DataFrame([trade()])
    before_snapshot, before_operations = snapshots.copy(deep=True), operations.copy(deep=True)
    actual = loader.build_reconciled_portfolio
    captured = {}
    def accounting(raw_snapshots, raw_operations, events, **kwargs):
        captured.update(snapshots=raw_snapshots, operations=raw_operations, events=events, **kwargs)
        return actual(raw_snapshots, raw_operations, events, **kwargs)
    monkeypatch.setattr(loader, "build_reconciled_portfolio", accounting)
    result = loader.load_reconciled_portfolio(Journal(), snapshots, operations)
    assert_frame_equal(captured["snapshots"], before_snapshot)
    assert result.positions.loc[result.positions.ticker == "AAA", "quantity"].iloc[0] == 12
    assert_frame_equal(snapshots, before_snapshot)
    assert_frame_equal(operations, before_operations)


def test_notes_rehydrate_same_day_phase_and_unknown_fees_without_overriding_money(monkeypatch):
    notes = loader.IMPORT_MARKER + json.dumps({"source_event_id": "synthetic-operation-1",
        "phase": "post_snapshot", "fee_quality": "unknown", "fx_quality": "reference",
        "price": 999, "fees": 99, "quantity": 999, "currency": "USD"})
    operations = pd.DataFrame([trade(executed_at="2026-10-13", notes=notes)])
    journal = Journal({"provenance": {"operations": [{"source_event_id": "synthetic-operation-1",
        "after_snapshot": True, "cost_quality": "documented_settlement", "settlement_amount_local": 999,
        "phase": "historical"}]}})
    enriched = loader._rehydrate_operations(operations, [journal.batches.iloc[0].metadata])
    row = enriched.iloc[0]
    assert row.phase == "post_snapshot"
    assert row.after_snapshot
    assert row.fee_quality == "unknown"
    assert row.cost_quality == "documented_settlement"
    assert row.price == 10 and row.quantity == 2 and row.fees == 0
    assert row.currency == "EUR" and row.settlement_amount_local == 20
    report = loader.load_reconciled_portfolio(journal, snapshot(), operations)
    assert report.positions.loc[report.positions.ticker == "AAA", "quantity"].iloc[0] == 12
    assert report.metrics_global["fees_eur"] is None


def test_json_metadata_and_stable_income_dedup_do_not_double_cash_or_dividends():
    metadata = {"events": [event()], "coverage": {"income_complete": True}}
    journal = Journal()
    journal.batches = pd.DataFrame([
        {"id": 1, "metadata": json.dumps(metadata), "created_at": "2026-10-15"},
        {"id": 2, "metadata": deepcopy(metadata), "created_at": "2026-10-16"},
    ])
    report = loader.load_reconciled_portfolio(journal, snapshot(), pd.DataFrame())
    assert len(report.cash_flows) == 1
    assert report.metrics_global["dividends_known_eur"] == 3
    assert report.metrics_global["cash_value_eur"] == 503
    assert journal.calls == [{}]


def test_conflicting_stable_income_is_pending_not_first_copy_wins():
    journal = Journal()
    journal.batches = pd.DataFrame([
        {"metadata": {"events": [event(amount=3, amount_eur=3)]}},
        {"metadata": {"events": [event(amount=4, amount_eur=4)]}},
    ])
    report = loader.load_reconciled_portfolio(journal, snapshot(), pd.DataFrame())
    assert report.cash_flows.empty
    assert report.metrics_global["cash_value_eur"] == 500
    assert any("identidad contradictoria" in issue for issue in report.issues)


def test_unidentified_identical_income_is_not_silently_content_deduplicated():
    events = [event(source_event_id=""), event(source_event_id="")]
    report = loader.load_reconciled_portfolio(Journal({"events": events}), snapshot(), pd.DataFrame())
    assert len(report.cash_flows) == 2
    assert report.metrics_global["dividends_known_eur"] == 6


def test_latest_dated_close_and_explicit_inverse_fx_are_current_valuation_only():
    prepared = {"AAA": pd.DataFrame({"close": [15, 12, 14]}, index=pd.to_datetime(["2026-10-15", "2026-10-13", "2026-10-14"]))}
    report = loader.load_reconciled_portfolio(Journal(), snapshot("USD"), pd.DataFrame(),
        prepared=prepared, rates_per_eur={"USD": 1.2}, reference_date="2026-10-14")
    row = report.positions.loc[report.positions.ticker == "AAA"].iloc[0]
    assert row.current_price == 14
    assert row.value_eur == pytest.approx(10 * 14 / 1.2)
    assert row.market_as_of == "2026-10-14"
    assert row.cost_basis_eur == 100
    assert report.metrics_global["total_return_pct"] is None
    assert report.monthly.total_return_pct.isna().all()


@pytest.mark.parametrize("rates", [{}, {"USD": 0}, {"USD": -2}, {"USD": float("nan")}])
def test_absent_or_invalid_fx_does_not_assume_usd_to_eur(rates):
    prepared = {"AAA": pd.DataFrame({"close": [15]}, index=pd.to_datetime(["2026-10-15"]))}
    report = loader.load_reconciled_portfolio(Journal(), snapshot("USD"), pd.DataFrame(), prepared=prepared, rates_per_eur=rates)
    row = report.positions.loc[report.positions.ticker == "AAA"].iloc[0]
    assert pd.isna(row.value_eur)
    assert row.valuation_status == "Cambio de moneda pendiente"


def test_missing_or_conflicting_native_currency_never_gets_a_usd_default():
    snapshots = snapshot()
    snapshots.loc[snapshots.analysis_ticker == "AAA", "currency"] = ""
    prepared = {"AAA": pd.DataFrame({"close": [15]}, index=pd.to_datetime(["2026-10-15"]))}
    report = loader.load_reconciled_portfolio(Journal(), snapshots, pd.DataFrame(), prepared=prepared, rates_per_eur={"USD": 1.2})
    row = report.positions.loc[report.positions.ticker == "AAA"].iloc[0]
    assert row.value_eur == 120
    assert "current_price" not in row or pd.isna(row.current_price)
    assert any("moneda nativa" in issue for issue in report.issues)


def test_tr_execution_currency_is_not_mistaken_for_quote_currency():
    snapshots = snapshot("USD")
    prepared = {"AAA": pd.DataFrame({"close": [24]}, index=pd.to_datetime(["2026-10-15"]))}
    prices = loader._price_map(snapshots, pd.DataFrame([trade()]), prepared, {"USD": 1.2}, None, [])
    assert prices[("Broker", "AAA", "EUR")]["currency"] == "USD"
    assert prices[("Broker", "AAA", "EUR")]["fx_rate_to_eur"] == pytest.approx(1 / 1.2)


def test_undated_numeric_index_is_not_a_1970_quote_and_future_quotes_are_filtered():
    prepared = {"AAA": pd.DataFrame({"close": [999]})}
    report = loader.load_reconciled_portfolio(Journal(), snapshot(), pd.DataFrame(), prepared=prepared)
    assert report.positions.loc[report.positions.ticker == "AAA", "value_eur"].iloc[0] == 120
    future = {"AAA": pd.DataFrame({"close": [999]}, index=pd.to_datetime(["2026-10-20"]))}
    report = loader.load_reconciled_portfolio(Journal(), snapshot(), pd.DataFrame(), prepared=future, reference_date="2026-10-15")
    assert report.positions.loc[report.positions.ticker == "AAA", "value_eur"].iloc[0] == 120


def test_only_whole_platform_scope_snapshots_are_history_marks(monkeypatch):
    rows = [holding(platform="A", value=100, day="2026-09-30"),
            holding(platform="B", value=200, day="2026-09-30"),
            holding(platform="A", value=110, day="2026-10-13")]
    snapshots = pd.DataFrame(rows)
    actual, captured = loader.build_reconciled_portfolio, {}
    def accounting(*args, **kwargs):
        captured.update(kwargs)
        return actual(*args, **kwargs)
    monkeypatch.setattr(loader, "build_reconciled_portfolio", accounting)
    report = loader.load_reconciled_portfolio(Journal(), snapshots, pd.DataFrame())
    marks = captured["valuation_history"]
    assert marks.to_dict("records") == [{"date": "2026-09-30", "value_eur": 300}]
    october = report.monthly.loc[report.monthly.period == "2026-10"].iloc[0]
    assert pd.isna(october.ending_value_eur)
    assert report.monthly.total_return_pct.isna().all()
    assert report.annual.total_return_pct.isna().all()


def test_import_pending_reference_issues_and_conflicts_remain_visible_without_raw_messages():
    metadata = {"issues": [{"code": "snapshot_wealth_reference_mismatch", "status": "pending",
                            "message": "SYNTHETIC_RAW_MESSAGE_NOT_FOR_LOGS"}],
                "pending_conflicts": [{"reason": "existing_snapshot_conflict", "asset_name": "Synthetic asset"}]}
    report = loader.load_reconciled_portfolio(Journal(metadata), snapshot(), pd.DataFrame())
    assert any("snapshot_wealth_reference_mismatch" in issue for issue in report.issues)
    assert any("existing_snapshot_conflict" in issue for issue in report.issues)
    assert not any("SYNTHETIC_RAW_MESSAGE_NOT_FOR_LOGS" in issue for issue in report.issues)


def test_optional_sectors_are_exact_mapping_default_unknown():
    report = loader.load_reconciled_portfolio(Journal(), snapshot(), pd.DataFrame(), sector_by_ticker={"AAA": "Tecnología"})
    assert report.positions.loc[report.positions.ticker == "AAA", "sector"].iloc[0] == "Tecnología"
    assert report.positions.loc[report.positions.ticker == "", "sector"].iloc[0] == ""
    assert report.metrics_by_asset.loc[report.metrics_by_asset.ticker == "AAA", "sector"].iloc[0] == "Tecnología"


def test_equivalent_json_numeric_and_timestamp_representations_are_one_income_event():
    journal = Journal()
    journal.batches = pd.DataFrame([
        {"metadata": {"events": [event(amount=3, amount_eur=3, executed_at="2026-10-14")]}},
        {"metadata": {"events": [event(amount=3.0, amount_eur=3.0, executed_at="2026-10-14T00:00:00")]}},
    ])
    report = loader.load_reconciled_portfolio(journal, snapshot(), pd.DataFrame())
    assert len(report.cash_flows) == 1
    assert report.metrics_global["dividends_known_eur"] == 3


def test_duplicate_quote_dates_choose_last_valid_close_without_ambiguous_series():
    prepared = {"AAA": pd.DataFrame({"close": [14, 15]}, index=pd.to_datetime(["2026-10-15", "2026-10-15"]))}
    report = loader.load_reconciled_portfolio(Journal(), snapshot(), pd.DataFrame(), prepared=prepared)
    assert report.positions.loc[report.positions.ticker == "AAA", "value_eur"].iloc[0] == 150


def test_missing_newest_coverage_does_not_inherit_older_complete_assertions():
    metadata = [{}, {"coverage": {"operations_complete": True, "per_platform": {"Broker": {"operations_complete": True}}}}]
    coverage = loader._merge_coverage(metadata)
    assert coverage["operations_complete"] is False
    assert coverage["per_platform"] == {}


def test_invalid_latest_close_is_pending_not_silently_replaced_with_older_close():
    prepared = {"AAA": pd.DataFrame({"close": [14, float("nan")]}, index=pd.to_datetime(["2026-10-14", "2026-10-15"]))}
    report = loader.load_reconciled_portfolio(Journal(), snapshot(), pd.DataFrame(), prepared=prepared)
    row = report.positions.loc[report.positions.ticker == "AAA"].iloc[0]
    assert pd.isna(row.value_eur)
    assert pd.isna(row.current_price)
    assert row.valuation_status == "Precio pendiente"
    assert row.market_as_of == "2026-10-15"
    assert any("último cierre preparado" in issue for issue in report.issues)


@pytest.mark.parametrize("day", ["2026-09-01", "2026-10-12"])
def test_old_quote_never_replaces_newer_documented_snapshot(day):
    prepared = {"AAA": pd.DataFrame({"close": [5]}, index=pd.to_datetime([day]))}
    report = loader.load_reconciled_portfolio(Journal(), snapshot(), pd.DataFrame(),
        prepared=prepared, reference_date="2026-10-15")
    row = report.positions.loc[report.positions.ticker == "AAA"].iloc[0]
    assert row.value_eur == 120
    assert row.market_as_of == "2026-10-13"
    assert row.valuation_status == "Valoración declarada de la fotografía"
    assert any("Cotización antigua" in issue for issue in report.issues)


def test_historical_currency_label_does_not_override_current_snapshot_quote_currency():
    old = snapshot("EUR")
    old["snapshot_date"] = "2026-09-30"
    current = snapshot("USD")
    frames = pd.concat([old, current], ignore_index=True)
    prepared = {"AAA": pd.DataFrame({"close": [24]}, index=pd.to_datetime(["2026-10-15"]))}
    report = loader.load_reconciled_portfolio(Journal(), frames, pd.DataFrame(),
        prepared=prepared, rates_per_eur={"USD": 1.2}, reference_date="2026-10-15")
    row = report.positions.loc[report.positions.ticker == "AAA"].iloc[0]
    assert row.value_eur == pytest.approx(200)
    assert row.market_as_of == "2026-10-15"


def test_missing_sector_remains_missing_for_position_review_capacity():
    from src.position_review import build_position_review_rows
    snapshots = pd.DataFrame([holding(ticker=f"SYN{i}", value=100) for i in range(10)])
    prepared = {f"SYN{i}": pd.DataFrame({"close": [10]}, index=pd.to_datetime(["2026-10-15"])) for i in range(10)}
    report = loader.load_reconciled_portfolio(Journal(), snapshots, pd.DataFrame(), prepared=prepared)
    assert set(report.positions.sector) == {""}
    rows = build_position_review_rows(report.positions, [], prepared, reference_date="2026-10-15")
    assert rows.capacity_eur.isna().all()
    assert not (rows.action == "Reducir concentración").any()
