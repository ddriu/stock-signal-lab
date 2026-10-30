"""Presentation guards never optimise or alter the original engines."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest

from src.analysis_data_quality import assess_analysis_data_quality
from src.analysis_policy import build_policy_snapshot, enrich_analysis_rows
from src.data_sources import download_sec_fundamental_snapshot, merge_fundamental_sources

VALUES = dict(revenueGrowth=.20, earningsGrowth=.30, operatingMargins=.25,
              returnOnEquity=.20, freeCashflow=100.)


def info(periodicity="quarterly", end="2026-06-30", **changes):
    return {**VALUES, **changes, "quoteType": "EQUITY",
            "_sources": {key: "SEC EDGAR" for key in VALUES},
            "_metric_periods": {key: {"source": "SEC EDGAR", "periodicity": periodicity,
                                     "period_end": end, "filed_at": "2026-08-05",
                                     "comparable": True} for key in VALUES}}


def test_current_verified_quarter_is_ok_and_inputs_are_not_mutated():
    original = info()
    before = deepcopy(original)
    result = assess_analysis_data_quality(original, as_of="2026-10-23")
    assert original == before
    assert result["status"] == "ok"
    assert not result["block_entry"]
    assert not result["affects_engine_scores"]
    assert result["not_a_sell_signal"]


def test_annual_observations_are_disclosed_not_automatically_expired():
    result = assess_analysis_data_quality(info("annual", "2025-12-31"), as_of="2026-10-23")
    assert result["status"] == "review"
    assert not result["block_entry"]
    assert any("cuentas anuales" in reason for reason in result["reasons"])
    assert result["metric_periods"]["revenueGrowth"]["period_end"] == "2025-12-31"


def test_recent_balance_does_not_refresh_stale_flow_metrics():
    source = info("annual", "2024-12-31")
    source["_official_period_end"] = "2026-09-30"
    result = assess_analysis_data_quality(source, as_of="2026-10-23")
    assert result["status"] == "blocked" and result["block_entry"]
    assert any("2024-12-31" in reason for reason in result["reasons"])


def test_generic_snapshot_date_does_not_invent_per_metric_dates():
    result = assess_analysis_data_quality({**VALUES, "_official_period_end": "2026-09-30"}, as_of="2026-10-23")
    assert result["status"] == "unknown" and result["block_entry"]
    assert result["metric_periods"]["revenueGrowth"]["period_end"] is None
    assert result["coverage"]["dated_metrics"] == 0
    assert result["coverage"]["undated_metrics"] == list(VALUES)
    assert "crecimiento de ingresos" in " ".join(result["reasons"])


def test_partial_provenance_describes_exactly_what_is_still_missing():
    source = info()
    del source["_metric_periods"]["earningsGrowth"]
    result = assess_analysis_data_quality(source, as_of="2026-10-23")
    assert result["block_entry"] and result["coverage"]["dated_metrics"] == 4
    assert result["coverage"]["undated_metrics"] == ["earningsGrowth"]
    assert "Falta el periodo verificable de: crecimiento del beneficio." in " ".join(result["reasons"])
    assert result["coverage"]["missing_metrics"] == []


@pytest.mark.parametrize("snapshot", [None, {}, {"symbol": "TEST"}, {"revenueGrowth": True},
                                      {"revenueGrowth": float("nan")}, {"_quick_mode": True}])
def test_insufficient_data_is_not_a_sell_signal(snapshot):
    result = assess_analysis_data_quality(snapshot, as_of="2026-10-23")
    assert result["status"] == "unknown" and result["block_entry"]
    assert result["not_a_sell_signal"]


def test_argx_like_mixed_concept_margin_requires_validation():
    source = info("annual", "2025-12-31", operatingMargins=28.94,
                  earningsGrowth=-3.445, revenueGrowth=1.475)
    source["totalRevenue"] = 36_415_000
    result = assess_analysis_data_quality(source, as_of="2026-10-23")
    assert result["block_entry"]
    assert any("100 %" in reason for reason in result["reasons"])
    assert source["operatingMargins"] == 28.94


def test_exceptional_earnings_base_is_not_promoted_as_sustainable_growth():
    source = info("annual", "2025-12-31", earningsGrowth=29.866)
    result = assess_analysis_data_quality(source, as_of="2026-10-23")
    assert result["block_entry"]
    assert any("efecto base" in reason for reason in result["reasons"])


def test_mismatched_component_periods_are_blocked():
    source = info()
    source["_metric_periods"]["operatingMargins"]["comparable"] = False
    result = assess_analysis_data_quality(source, as_of="2026-10-23")
    assert result["block_entry"]
    assert any("no comparables" in reason for reason in result["reasons"])


def test_comparability_key_ignores_query_clock_but_detects_metric_and_period_changes():
    source = info()
    first = assess_analysis_data_quality(source, as_of="2026-10-22")
    second = assess_analysis_data_quality(source, as_of="2026-10-23")
    assert first["comparability_key"] == second["comparability_key"]
    annual = assess_analysis_data_quality(info("annual", "2025-12-31"), as_of="2026-10-23")
    assert annual["comparability_key"] != second["comparability_key"]
    source["earningsGrowth"] = .31
    changed = assess_analysis_data_quality(source, as_of="2026-10-23")
    assert changed["comparability_key"] != second["comparability_key"]


def test_funds_do_not_require_nonexistent_corporate_accounts():
    result = assess_analysis_data_quality({"quoteType": "ETF"}, as_of="2026-10-23")
    assert not result["block_entry"] and result["status"] == "review"


def test_policy_adds_guard_but_keeps_raw_score_label_and_position():
    row = {"Ticker": "TEST", "Fecha": date(2026, 10, 23), "Si ya la tienes": "Mantener"}
    growth = SimpleNamespace(score=88, growth_score=95, momentum_score=81, context_score=86,
                             label="Entrada fuerte", confidence_pct=76, sector_key="biotech")
    baseline = build_policy_snapshot(row, growth_result=growth, held=True)
    guarded = build_policy_snapshot(row, growth_result=growth, held=True,
                                    fundamental_info=info("annual", "2025-12-31", earningsGrowth=29.866))
    assert guarded["primary"] == baseline["primary"]
    assert guarded["position"] == baseline["position"]
    assert guarded["data_quality"]["block_entry"]
    enriched = enrich_analysis_rows([row], growth_results={"TEST": growth},
                                    fundamental_info={"TEST": info()})
    assert enriched[0]["Política de análisis"]["data_quality"]["status"] == "ok"


def test_unrelated_private_fields_do_not_enter_quality_metadata():
    source = info()
    source["owner"] = "private-owner@example.invalid"
    source["_metric_periods"]["revenueGrowth"]["private_note"] = "private secret"
    result = assess_analysis_data_quality(source, as_of="2026-10-23")
    assert "private" not in repr(result)


def test_official_override_does_not_inherit_a_different_providers_period():
    yahoo = {"revenueGrowth": .25, "_metric_periods": {"revenueGrowth": {"period_end": "2026-09-30"}}}
    merged = merge_fundamental_sources(yahoo, {"revenueGrowth": .20, "_official_source": "SEC EDGAR"})
    assert merged["revenueGrowth"] == .20
    assert "revenueGrowth" not in merged["_metric_periods"]


def test_sec_provenance_tracks_actual_annual_facts_not_latest_balance(monkeypatch):
    def flow(value, year):
        return {"start": f"{year}-01-01", "end": f"{year}-12-31", "val": value,
                "form": "10-K", "filed": f"{year + 1}-02-20"}
    def fact(values):
        return {"units": {"USD": values}}
    facts = {"entityName": "Synthetic Inc.", "facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": fact([flow(100, 2024), flow(120, 2025)]),
        "NetIncomeLoss": fact([flow(10, 2024), flow(12, 2025)]),
        "OperatingIncomeLoss": fact([flow(20, 2024), flow(25, 2025)]),
        "StockholdersEquity": fact([{"end": "2026-06-30", "val": 100, "form": "10-Q", "filed": "2026-08-05"}]),
    }}}
    monkeypatch.setattr("src.data_sources._sec_ticker_map", lambda user_agent: {"TEST": 123})
    monkeypatch.setattr("src.data_sources._get_json", lambda url, headers: facts)
    result = download_sec_fundamental_snapshot("TEST")
    assert result["revenueGrowth"] == pytest.approx(.20)
    assert result["_official_period_end"] == "2026-06-30"
    metadata = result["_metric_periods"]["revenueGrowth"]
    assert metadata["period_end"] == "2025-12-31" and metadata["periodicity"] == "annual"
    assert metadata["components"][0]["comparison_base"]
    assert metadata["components"][-1]["concept"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
