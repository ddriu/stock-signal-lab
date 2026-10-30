"""Offline regression tests for value/period pairing, not investment tuning."""
from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest

from src import data_loader
from src.analysis_data_quality import assess_analysis_data_quality
from src.data_sources import _metric_provenance, download_sec_fundamental_snapshot


SUMMARY = {
    "symbol": "TEST", "quoteType": "EQUITY", "currency": "USD", "financialCurrency": "USD",
    "revenueGrowth": .91, "earningsGrowth": .88, "operatingMargins": .99,
    "returnOnEquity": .77, "freeCashflow": 999., "forwardPE": 28.,
}


class StatementTicker:
    def __init__(self, info=None):
        self.info = deepcopy(SUMMARY if info is None else info)
        self.calls = []
        self.income = pd.DataFrame(
            {"2025-12-31": [120., 24., 30.], "2024-12-31": [100., 20., 25.]},
            index=["TotalRevenue", "NetIncome", "OperatingIncome"],
        )
        self.balance = pd.DataFrame(
            {"2025-12-31": [110., 80., 40., 30.], "2024-12-31": [90., 70., 35., 28.]},
            index=["StockholdersEquity", "CurrentAssets", "CurrentLiabilities", "TotalDebt"],
        )
        self.cashflow = pd.DataFrame(
            {"2025-12-31": [18., 25., -7.], "2024-12-31": [14., 19., -5.]},
            index=["FreeCashFlow", "OperatingCashFlow", "CapitalExpenditure"],
        )

    def get_info(self):
        return deepcopy(self.info)

    def get_income_stmt(self, **kwargs):
        self.calls.append("income")
        return self.income

    def get_balance_sheet(self, **kwargs):
        self.calls.append("balance")
        return self.balance

    def get_cash_flow(self, **kwargs):
        self.calls.append("cashflow")
        return self.cashflow


def snapshot(monkeypatch, stock, official=None):
    monkeypatch.setattr(data_loader.yf, "Ticker", lambda symbol: stock)
    monkeypatch.setattr(data_loader, "download_sec_fundamental_snapshot", lambda symbol: official or {})
    return data_loader.download_fundamental_snapshot("TEST")


def test_complete_but_undated_summary_triggers_dated_recalculation(monkeypatch):
    stock = StatementTicker()
    result = snapshot(monkeypatch, stock)
    assert stock.calls == ["income", "balance", "cashflow"]
    assert result["revenueGrowth"] == pytest.approx(.20)
    assert result["earningsGrowth"] == pytest.approx(.20)
    assert result["operatingMargins"] == pytest.approx(.25)
    assert result["returnOnEquity"] == pytest.approx(.24)
    assert result["freeCashflow"] == 18.
    assert result["forwardPE"] == 28.
    assert result["_metric_periods"]["revenueGrowth"]["period_end"] == "2025-12-31"
    assert result["_metric_periods"]["revenueGrowth"]["periodicity"] == "annual"
    assert result["_sources"]["revenueGrowth"] == "Yahoo Financial Statements"
    assert result["_sources"]["forwardPE"] == "Yahoo Finance"
    assert result["_snapshot_version"] == data_loader.FUNDAMENTAL_SNAPSHOT_VERSION
    assert not result["_provenance_recovery"]["unresolved_metrics"]
    quality = assess_analysis_data_quality(result, "2026-10-30")
    assert quality["block_entry"] is False
    assert any("anuales" in reason for reason in quality["reasons"])


def test_partial_statements_do_not_certify_unrecovered_summary_ratios(monkeypatch):
    stock = StatementTicker()
    stock.cashflow = pd.DataFrame()
    result = snapshot(monkeypatch, stock)
    assert result["revenueGrowth"] == pytest.approx(.20)
    assert result["freeCashflow"] == SUMMARY["freeCashflow"]
    assert "freeCashflow" not in result["_metric_periods"]
    assert result["_sources"]["freeCashflow"] == "Yahoo Finance"
    assert "freeCashflow" in result["_provenance_recovery"]["unresolved_metrics"]
    assert assess_analysis_data_quality(result, "2026-10-30")["block_entry"] is True


def test_general_quarter_timestamp_is_never_attached_to_existing_ratio(monkeypatch):
    stock = StatementTicker({**SUMMARY, "mostRecentQuarter": 1788134400})
    stock.income = stock.balance = stock.cashflow = pd.DataFrame()
    result = snapshot(monkeypatch, stock)
    assert result["revenueGrowth"] == .91
    assert not result["_metric_periods"]
    assert result["_provenance_recovery"]["unresolved_metrics"]
    assert assess_analysis_data_quality(result, "2026-10-30")["block_entry"] is True


def test_sec_value_and_its_period_win_over_dated_yahoo_fallback(monkeypatch):
    stock = StatementTicker()
    official = {
        "revenueGrowth": .30, "_official_source": "SEC EDGAR",
        "_metric_periods": {"revenueGrowth": {
            "source": "SEC EDGAR", "periodicity": "annual", "period_end": "2025-09-30",
            "comparable": True, "components": [],
        }},
    }
    result = snapshot(monkeypatch, stock, official)
    assert result["revenueGrowth"] == .30
    assert result["_metric_periods"]["revenueGrowth"]["period_end"] == "2025-09-30"
    assert result["_sources"]["revenueGrowth"] == "SEC EDGAR"
    assert result["earningsGrowth"] == pytest.approx(.20)


def test_complete_dated_official_values_avoid_redundant_yahoo_calls(monkeypatch):
    stock = StatementTicker()
    official = {key: value for key, value in SUMMARY.items() if key in {
        "revenueGrowth", "earningsGrowth", "operatingMargins", "returnOnEquity", "freeCashflow"}}
    official["_metric_periods"] = {key: {
        "source": "SEC EDGAR", "periodicity": "annual", "period_end": "2025-12-31", "comparable": True,
    } for key in official}
    result = snapshot(monkeypatch, stock, official)
    assert stock.calls == []
    assert result["_provenance_recovery"]["attempted"] is False
    assert not result["_provenance_recovery"]["unresolved_metrics"]


@pytest.mark.parametrize("quote_type", ["ETF", "MUTUALFUND"])
def test_fund_does_not_fetch_corporate_statements(monkeypatch, quote_type):
    stock = StatementTicker({"quoteType": quote_type})
    result = snapshot(monkeypatch, stock)
    assert not stock.calls
    assert result["_provenance_recovery"]["attempted"] is False
    assert not result["_provenance_recovery"]["unresolved_metrics"]
    assert assess_analysis_data_quality(result, "2026-10-30")["block_entry"] is False


def test_statement_request_failure_preserves_other_statement_fallbacks(monkeypatch):
    stock = StatementTicker()
    def unavailable(**kwargs):
        raise TimeoutError("secret must not appear in public evidence")
    stock.get_income_stmt = unavailable
    result = snapshot(monkeypatch, stock)
    assert result["freeCashflow"] == 18.
    assert result["revenueGrowth"] == SUMMARY["revenueGrowth"]
    assert "revenueGrowth" not in result["_metric_periods"]
    assert any("TimeoutError" in item for item in result["_warnings"])
    assert not any("secret" in item for item in result["_warnings"])


def test_operating_cashflow_without_capex_is_not_free_cashflow():
    stock = StatementTicker()
    stock.cashflow = stock.cashflow.loc[["OperatingCashFlow"]]
    result = data_loader._yahoo_statement_snapshot(stock)
    assert result["operatingCashflow"] == 25.
    assert "freeCashflow" not in result
    assert "freeCashflow" not in result["_metric_periods"]


def test_fcf_from_cash_and_capex_keeps_both_matching_components():
    stock = StatementTicker()
    stock.cashflow = stock.cashflow.drop(index="FreeCashFlow")
    result = data_loader._yahoo_statement_snapshot(stock)
    assert result["freeCashflow"] == 18.
    metadata = result["_metric_periods"]["freeCashflow"]
    assert metadata["comparable"] is True
    assert {item["role"] for item in metadata["components"]} == {"OperatingCashFlow", "CapitalExpenditure"}


def test_nonconsecutive_growth_comparison_is_disclosed_and_blocked(monkeypatch):
    stock = StatementTicker()
    stock.income = stock.income.rename(columns={"2024-12-31": "2023-12-31"})
    result = snapshot(monkeypatch, stock)
    assert result["_metric_periods"]["revenueGrowth"]["comparable"] is False
    assert assess_analysis_data_quality(result, "2026-10-30")["block_entry"] is True


def test_mismatched_periods_and_accounting_currencies_are_not_certified(monkeypatch):
    stock = StatementTicker()
    stock.income.attrs["currency"] = "USD"
    stock.balance.attrs["currency"] = "EUR"
    result = snapshot(monkeypatch, stock)
    metadata = result["_metric_periods"]["returnOnEquity"]
    assert metadata["comparable"] is False
    assert {item["unit"] for item in metadata["components"]} == {"USD", "EUR"}
    assert assess_analysis_data_quality(result, "2026-10-30")["block_entry"] is True


def test_income_and_equity_from_different_years_cannot_certify_roe(monkeypatch):
    stock = StatementTicker()
    stock.balance = stock.balance.rename(columns={"2025-12-31": "2024-12-31", "2024-12-31": "2023-12-31"})
    result = snapshot(monkeypatch, stock)
    assert result["_metric_periods"]["returnOnEquity"]["comparable"] is False
    assert assess_analysis_data_quality(result, "2026-10-30")["block_entry"] is True


@pytest.mark.parametrize("prior_year", [None, "2022-12-31"])
def test_roe_requires_two_consecutive_equity_closings(monkeypatch, prior_year):
    stock = StatementTicker()
    if prior_year is None:
        stock.balance = stock.balance.drop(columns="2024-12-31")
    else:
        stock.balance = stock.balance.rename(columns={"2024-12-31": prior_year})
    result = snapshot(monkeypatch, stock)
    metadata = result["_metric_periods"]["returnOnEquity"]
    assert metadata["period_end"] == "2025-12-31"
    assert metadata["comparable"] is False
    assert assess_analysis_data_quality(result, "2026-10-30")["block_entry"] is True


def test_stale_annual_recovery_does_not_make_a_candidate_current(monkeypatch):
    stock = StatementTicker()
    for attribute in ("income", "balance", "cashflow"):
        frame = getattr(stock, attribute)
        setattr(stock, attribute, frame.rename(columns={"2025-12-31": "2023-12-31", "2024-12-31": "2022-12-31"}))
    result = snapshot(monkeypatch, stock)
    quality = assess_analysis_data_quality(result, "2026-10-30")
    assert result["_metric_periods"]["revenueGrowth"]["period_end"] == "2023-12-31"
    assert quality["block_entry"] is True
    assert any("desactualizado" in item for item in quality["reasons"])


def test_quotation_currency_is_not_assumed_to_be_statement_currency(monkeypatch):
    stock = StatementTicker({key: value for key, value in SUMMARY.items() if key != "financialCurrency"})
    result = snapshot(monkeypatch, stock)
    assert all(item["unit"] == "" for item in result["_metric_periods"]["revenueGrowth"]["components"])


def test_invalid_or_conflicting_dates_do_not_get_paired_with_valid_value():
    stock = StatementTicker()
    stock.income = pd.DataFrame(
        {"invalid": [999.], "2025-12-31": [120.], "2024-12-31": [100.]}, index=["TotalRevenue"])
    result = data_loader._yahoo_statement_snapshot(stock)
    assert result["revenueGrowth"] == pytest.approx(.20)
    assert result["_metric_periods"]["revenueGrowth"]["components"][0]["value"] == 120.
    stock.income = pd.DataFrame([[120., 999., 100.]], index=["TotalRevenue"],
                                columns=["2025-12-31", "2025-12-31", "2024-12-31"])
    result = data_loader._yahoo_statement_snapshot(stock)
    assert "revenueGrowth" not in result


def test_conflicting_duplicate_concepts_are_not_silently_accepted():
    stock = StatementTicker()
    stock.income = pd.DataFrame(
        {"2025-12-31": [120., 999.], "2024-12-31": [100., 100.]},
        index=["TotalRevenue", "TotalRevenue"],
    )
    result = data_loader._yahoo_statement_snapshot(stock)
    assert "revenueGrowth" not in result


def test_changed_period_and_value_changes_comparability_not_engine_parameters(monkeypatch):
    stock = StatementTicker()
    before = assess_analysis_data_quality(SUMMARY, "2026-10-30")
    after = assess_analysis_data_quality(snapshot(monkeypatch, stock), "2026-10-30")
    assert before["comparability_key"] != after["comparability_key"]
    assert before["affects_engine_scores"] is after["affects_engine_scores"] is False


def _sec_roe_snapshot(monkeypatch, equities):
    def annual(value, year):
        return {"start": f"{year}-01-01", "end": f"{year}-12-31", "val": value,
                "form": "10-K", "filed": f"{year + 1}-02-20"}
    facts = {"entityName": "Synthetic Inc.", "facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [annual(20, 2024), annual(24, 2025)]}},
        "StockholdersEquity": {"units": {"USD": equities}},
    }}}
    monkeypatch.setattr("src.data_sources._sec_ticker_map", lambda user_agent: {"TEST": 123})
    monkeypatch.setattr("src.data_sources._get_json", lambda url, headers: facts)
    return download_sec_fundamental_snapshot("TEST")


@pytest.mark.parametrize("prior_year, comparable", [(2024, True), (2022, False)])
def test_sec_roe_metadata_requires_consecutive_aligned_equity_years(monkeypatch, prior_year, comparable):
    equities = [{"end": f"{year}-12-31", "val": value, "form": "10-K",
                 "filed": f"{year + 1}-02-20"}
                for year, value in [(prior_year, 90), (2025, 110)]]
    result = _sec_roe_snapshot(monkeypatch, equities)
    assert result["returnOnEquity"] == pytest.approx(.24)  # numerical formula unchanged
    assert result["_metric_periods"]["returnOnEquity"]["comparable"] is comparable


def test_sec_roe_metadata_discloses_single_instant_fallback_and_blocks_it(monkeypatch):
    equities = [{"end": "2026-06-30", "val": 120, "form": "10-Q", "filed": "2026-08-05"}]
    result = _sec_roe_snapshot(monkeypatch, equities)
    assert result["returnOnEquity"] == pytest.approx(.20)  # existing single-value fallback
    metadata = result["_metric_periods"]["returnOnEquity"]
    assert metadata["comparable"] is False
    assert any(item["role"] == "equity" and item["end"] == "2026-06-30" for item in metadata["components"])
    assert metadata["period_end"] == "2025-12-31"  # not the newer unrelated balance


def test_sec_roe_metadata_rejects_two_consecutive_equities_from_wrong_income_year(monkeypatch):
    equities = [{"end": f"{year}-12-31", "val": value, "form": "10-K",
                 "filed": f"{year + 1}-02-20"}
                for year, value in [(2023, 90), (2024, 110)]]
    result = _sec_roe_snapshot(monkeypatch, equities)
    assert result["returnOnEquity"] == pytest.approx(.24)
    assert result["_metric_periods"]["returnOnEquity"]["comparable"] is False


def test_sec_growth_metadata_does_not_certify_a_single_comparison_observation():
    metadata = _metric_provenance(("revenue", [{"end": "2025-12-31", "val": 120, "_unit": "USD"}]),
                                  periodicity="annual", growth_comparison=True)
    assert metadata["comparable"] is False


def test_sec_provenance_uses_publication_date_of_all_facts_actually_used():
    metadata = _metric_provenance(("revenue", [
        {"end": "2024-12-31", "val": 100, "_unit": "USD", "filed": "2026-06-30"},
        {"end": "2025-12-31", "val": 120, "_unit": "USD", "filed": "2026-02-20"},
    ]), periodicity="annual", growth_comparison=True)
    assert metadata["filed_at"] == "2026-06-30"


@pytest.mark.parametrize("has_current_ifrs15", [True, False])
def test_sec_ifrs15_revenue_alias_does_not_use_legacy_2015_revenue_for_2025_margin(monkeypatch, has_current_ifrs15):
    def annual(value, year, *, filed="2026-02-26"):
        return {"start": f"{year}-01-01", "end": f"{year}-12-31", "val": value,
                "form": "20-F", "filed": filed}
    def fact(values):
        return {"units": {"USD": values}}
    # Public-fact-shaped fixture: IFRS 15 has a distinct general revenue concept.
    # No freight/subcategory facts are summed or spliced into the old series.
    concepts = {
        "Revenue": fact([annual(800_000_000, 2015, filed="2018-03-01")]),
        "ProfitLoss": fact([annual(611_500_000, 2024), annual(286_000_000, 2025)]),
        "ProfitLossFromOperatingActivities": fact([annual(658_800_000, 2024), annual(356_300_000, 2025)]),
    }
    if has_current_ifrs15:
        concepts["RevenueFromContractsWithCustomers"] = fact([
            annual(1_559_200_000, 2024), annual(1_339_500_000, 2025)])
    facts = {"entityName": "Synthetic IFRS reporter", "facts": {"ifrs-full": concepts}}
    monkeypatch.setattr("src.data_sources._sec_ticker_map", lambda user_agent: {"TEST": 123})
    monkeypatch.setattr("src.data_sources._get_json", lambda url, headers: facts)
    result = download_sec_fundamental_snapshot("TEST")
    margin_period = result["_metric_periods"]["operatingMargins"]
    quality = assess_analysis_data_quality(result, "2026-10-30")
    if has_current_ifrs15:
        assert result["totalRevenue"] == 1_339_500_000
        assert result["revenueGrowth"] == pytest.approx(1_339_500_000 / 1_559_200_000 - 1)
        assert result["operatingMargins"] == pytest.approx(356_300_000 / 1_339_500_000)
        assert margin_period["period_end"] == "2025-12-31"
        assert margin_period["comparable"] is True
        assert quality["block_entry"] is False
        assert {item["concept"] for item in result["_metric_periods"]["revenueGrowth"]["components"]} == {
            "RevenueFromContractsWithCustomers"}
    else:
        assert result["totalRevenue"] == 800_000_000
        assert margin_period["period_end"] == "2015-12-31"
        assert margin_period["comparable"] is False
        assert quality["block_entry"] is True  # absent modern facts must remain pending
