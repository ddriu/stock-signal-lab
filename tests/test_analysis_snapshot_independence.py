"""Batch snapshots preserve independent observed strategies, never fake notes."""
from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from config import StrategyConfig
from src.analysis_policy import build_policy_snapshot, decode_policy_note
from src.analysis_snapshots import persist_analysis_batch_snapshots
from src.journal import TradingJournal
from src.signal_engine import SignalResult


SESSION = "2026-11-03"


def policy(*, ticker="MINE", day=SESSION, score=85):
    return build_policy_snapshot({"Ticker": ticker, "Fecha": day},
        growth_result=SimpleNamespace(score=score, confidence_pct=90,
            label="Entrada fuerte", growth_score=80, momentum_score=90, context_score=80,
            sector_key="technology", as_of=pd.Timestamp(day), positive_factors=(),
            risk_factors=(), manual_checks=()),
        position_context={"thesis": "Tesis conservada", "horizon": "Medio plazo"}, held=True)


def setup_case(monkeypatch, tmp_path):
    journal = TradingJournal(tmp_path / "independent-batch.sqlite", owner="synthetic")
    frames = {"MINE": pd.DataFrame({"close": [100.]}, index=pd.DatetimeIndex([SESSION]))}
    monkeypatch.setattr("src.analysis_snapshots.evaluate_latest_signal",
        lambda frame, config, *, ticker: SignalResult(ticker=ticker, as_of=pd.Timestamp(SESSION),
            score=50, label="Esperar", position_label="Mantener", explanation="Señal original",
            positive_factors=(), risk_factors=()))
    return journal, frames


def persist(journal, frames, policies, *, fundamentals=None, opportunities=None):
    return persist_analysis_batch_snapshots(journal, ["MINE"], frames,
        fundamentals or {}, {}, {}, {}, opportunities or {}, StrategyConfig(),
        policy_snapshots=policies)


def test_batch_stores_sealed_primary_without_optional_comparison_and_is_idempotent(monkeypatch, tmp_path):
    journal, frames = setup_case(monkeypatch, tmp_path)
    original_policy = policy()
    untouched = deepcopy(original_policy)
    before = pd.Timestamp.now().floor("s")
    first = persist(journal, frames, {"MINE": original_policy})
    second = persist(journal, frames, {"MINE": original_policy})
    assert first.saved_tickers == ("MINE",) and first.failures == ()
    assert second.existing_tickers == ("MINE",) and second.saved_tickers == ()
    saved = journal.list_analysis_snapshots()
    assert len(saved) == 1
    snapshot = saved.iloc[0]
    assert all(pd.isna(snapshot[name]) for name in (
        "opportunity_score", "company_score", "valuation_score", "relative_score", "risk_score", "confidence_pct"))
    assert snapshot.entry_score == 50  # Not replaced by primary 85 or a synthetic 0.
    stored_policy = decode_policy_note(snapshot.note)
    assert stored_policy == original_policy == untouched
    assert stored_policy["primary"]["score"] == 85
    assert stored_policy["position"]["thesis"] == "Tesis conservada"
    assert pd.Timestamp(snapshot.created_at) >= before
    assert pd.Timestamp(snapshot.analyzed_at).date() == pd.Timestamp(SESSION).date()
    assert journal.list_operations().empty


@pytest.mark.parametrize("change", ["absent", "score_missing", "wrong_ticker", "wrong_day", "bad_identity", "partial"])
def test_incomplete_or_mismatched_primary_does_not_bypass_missing_results(monkeypatch, tmp_path, change):
    journal, frames = setup_case(monkeypatch, tmp_path)
    observed = (None if change == "absent" else
        policy(ticker="OTHER") if change == "wrong_ticker" else
        policy(day="2026-11-02") if change == "wrong_day" else
        policy(score=None) if change == "score_missing" else policy())
    if change == "bad_identity":
        observed["snapshot_id"] = "f" * 64
    if change == "partial":
        observed["primary"]["data_status"] = "partial"
    result = persist(journal, frames, {"MINE": observed} if observed else {})
    assert result.saved_count == 0 and result.failures
    assert journal.list_analysis_snapshots().empty
    assert journal.list_operations().empty


def test_primary_does_not_permit_absent_prepared_price_or_replace_available_subscores(monkeypatch, tmp_path):
    journal, frames = setup_case(monkeypatch, tmp_path)
    observed = policy()
    rejected = persist(journal, {}, {"MINE": observed})
    assert rejected.saved_count == 0 and "prepared" in rejected.failures[0].reason
    accepted = persist(journal, frames, {"MINE": observed},
        fundamentals={"MINE": SimpleNamespace(score=72, sector="Technology")})
    assert accepted.saved_tickers == ("MINE",)
    snapshot = journal.list_analysis_snapshots().iloc[0]
    assert snapshot.company_score == 72
    assert pd.isna(snapshot.opportunity_score)
    assert decode_policy_note(snapshot.note)["primary"]["score"] == 85
