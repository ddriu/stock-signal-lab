"""Nullable comparison is transmitted as unknown, not a substitute score."""
import pandas as pd
import pytest

from src.supabase_journal import SupabaseTradingJournal
from src.opportunity_catalog import build_opportunity_catalog


def values():
    return dict(ticker="PRIMARY", analyzed_at="2026-11-03", price=105,
        opportunity_score=None, company_score=None, entry_score=85,
        valuation_score=None, relative_score=None, risk_score=70,
        opportunity_label="", entry_label="Entrada fuerte", position_label="Mantener",
        note="Synthetic independent policy")


def test_supabase_transmits_null_comparison_without_changing_primary_score(monkeypatch):
    from test_supabase_journal import FakeResponse
    calls=[]
    def request(method,url,**kwargs):
        calls.append((method,kwargs))
        return FakeResponse([{"id":123}])
    monkeypatch.setattr("src.supabase_journal.requests.request",request)
    journal=SupabaseTradingJournal("https://example.supabase.co","sb_secret_test","synthetic")
    assert journal.add_analysis_snapshot(**values()) == 123
    body=calls[0][1]["json"]
    assert body["owner"] == "synthetic"
    assert body["opportunity_score"] is None and body["entry_score"] == 85
    assert body["note"] == values()["note"]


@pytest.mark.parametrize("bad",[-1,101])
def test_nullable_contract_still_rejects_invalid_non_null_scores_without_http(monkeypatch,bad):
    monkeypatch.setattr("src.supabase_journal.requests.request",lambda *a,**k:pytest.fail("No HTTP for invalid score"))
    journal=SupabaseTradingJournal("https://example.supabase.co","sb_secret_test","synthetic")
    with pytest.raises(ValueError):
        journal.add_analysis_snapshot(**{**values(),"opportunity_score":bad})


def test_catalog_preserves_unknown_comparison_and_valid_primary_reading():
    saved=pd.DataFrame([{**values(),"id":1,"created_at":"2026-11-04T18:00:00Z"}])
    row=build_opportunity_catalog(["PRIMARY"],saved,[])[0]
    assert row["Oportunidad"] is None
    assert row["Momento entrada"] == 85
    assert row["Lectura entrada"] == "Entrada fuerte"
