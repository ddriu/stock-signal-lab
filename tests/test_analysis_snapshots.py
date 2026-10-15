from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from config import StrategyConfig
from src.analysis_snapshots import (
    DEFAULT_BATCH_SNAPSHOT_NOTE,
    AnalysisSnapshotFailure,
    persist_analysis_batch_snapshots,
)
from src.signal_engine import SignalResult
from src.analysis_policy import build_policy_snapshot, decode_policy_note, encode_policy_note


class FakeJournal:
    def __init__(self, existing: list[dict[str, object]] | None = None) -> None:
        self.snapshots = list(existing or [])
        self.list_calls = 0

    def list_analysis_snapshots(self, ticker: str | None = None) -> pd.DataFrame:
        self.list_calls += 1
        rows = self.snapshots
        if ticker is not None:
            normalized = ticker.strip().upper()
            rows = [
                row
                for row in rows
                if str(row.get("ticker") or "").strip().upper() == normalized
            ]
        if not rows:
            return pd.DataFrame(columns=["ticker", "analyzed_at"])
        return pd.DataFrame(rows)

    def add_analysis_snapshot(self, **values: object) -> int:
        self.snapshots.append(values)
        return len(self.snapshots)


def _prepared(*tickers: str) -> dict[str, pd.DataFrame]:
    return {
        ticker: pd.DataFrame(
            {"close": [120.0 + position]},
            index=pd.DatetimeIndex(["2026-09-26"]),
        )
        for position, ticker in enumerate(tickers)
    }


def _results(*tickers: str) -> tuple[dict[str, object], ...]:
    fundamentals = {
        ticker: SimpleNamespace(score=91, sector="Technology") for ticker in tickers
    }
    valuations = {ticker: SimpleNamespace(score=63) for ticker in tickers}
    relatives = {ticker: SimpleNamespace(score=77) for ticker in tickers}
    risks = {ticker: SimpleNamespace(score=68) for ticker in tickers}
    opportunities = {
        ticker: SimpleNamespace(
            score=82,
            confidence_pct=87,
            label="Oportunidad destacada",
            explanation=f"Explicación completa de {ticker}.",
        )
        for ticker in tickers
    }
    return fundamentals, valuations, relatives, risks, opportunities


def _signal(ticker: str) -> SignalResult:
    return SignalResult(
        ticker=ticker,
        as_of=pd.Timestamp("2026-09-26"),
        score=79,
        label="Entrada interesante",
        position_label="Mantener",
        explanation=f"Señal completa de {ticker}.",
        positive_factors=(),
        risk_factors=(),
    )


def test_batch_snapshot_persists_exact_confidence_signal_scores_and_explanation(
    monkeypatch,
) -> None:
    journal = FakeJournal()
    strategy = StrategyConfig()
    fundamentals, valuations, relatives, risks, opportunities = _results("AAA")
    calls: list[tuple[str, StrategyConfig]] = []

    def evaluate(frame, config, *, ticker: str) -> SignalResult:
        del frame
        calls.append((ticker, config))
        return _signal(ticker)

    monkeypatch.setattr("src.analysis_snapshots.evaluate_latest_signal", evaluate)

    result = persist_analysis_batch_snapshots(
        journal,
        ["aaa"],
        _prepared("AAA"),
        fundamentals,
        valuations,
        relatives,
        risks,
        opportunities,
        strategy,
    )

    assert result.saved_tickers == ("AAA",)
    assert result.existing_tickers == ()
    assert result.failures == ()
    assert calls == [("AAA", strategy)]
    assert journal.list_calls == 1
    assert len(journal.snapshots) == 1
    snapshot = journal.snapshots[0]
    assert snapshot["ticker"] == "AAA"
    assert snapshot["analyzed_at"] == pd.Timestamp("2026-09-26")
    assert snapshot["price"] == 120.0
    assert snapshot["opportunity_score"] == 82
    assert snapshot["confidence_pct"] == 87
    assert snapshot["company_score"] == 91
    assert snapshot["entry_score"] == 79
    assert snapshot["valuation_score"] == 63
    assert snapshot["relative_score"] == 77
    assert snapshot["risk_score"] == 68
    assert snapshot["opportunity_label"] == "Oportunidad destacada"
    assert snapshot["entry_label"] == "Entrada interesante"
    assert snapshot["position_label"] == "Mantener"
    assert snapshot["sector"] == "Technology"
    assert snapshot["explanation"] == "Explicación completa de AAA."
    assert snapshot["note"] == DEFAULT_BATCH_SNAPSHOT_NOTE


def test_batch_snapshot_is_idempotent_per_normalized_ticker_and_signal_day(
    monkeypatch,
) -> None:
    journal = FakeJournal(
        [{"ticker": "aaa", "analyzed_at": "2026-09-26T18:30:00+02:00"}]
    )
    fundamentals, valuations, relatives, risks, opportunities = _results("AAA", "BBB")
    monkeypatch.setattr(
        "src.analysis_snapshots.evaluate_latest_signal",
        lambda frame, config, *, ticker: _signal(ticker),
    )
    arguments = (
        journal,
        ["aaa", "AAA", "bbb"],
        _prepared("AAA", "BBB"),
        fundamentals,
        valuations,
        relatives,
        risks,
        opportunities,
        StrategyConfig(),
    )

    first = persist_analysis_batch_snapshots(*arguments)
    second = persist_analysis_batch_snapshots(*arguments)

    assert first.saved_tickers == ("BBB",)
    assert first.existing_tickers == ("AAA",)
    assert second.saved_tickers == ()
    assert second.existing_tickers == ("AAA", "BBB")
    assert len(journal.snapshots) == 2
    assert journal.list_calls == 2


def test_batch_snapshot_reports_missing_results_without_blocking_other_tickers(
    monkeypatch,
) -> None:
    journal = FakeJournal()
    fundamentals, valuations, relatives, risks, opportunities = _results("GOOD")
    monkeypatch.setattr(
        "src.analysis_snapshots.evaluate_latest_signal",
        lambda frame, config, *, ticker: _signal(ticker),
    )

    result = persist_analysis_batch_snapshots(
        journal,
        ["MISSING", "GOOD"],
        _prepared("MISSING", "GOOD"),
        fundamentals,
        valuations,
        relatives,
        risks,
        opportunities,
        StrategyConfig(),
    )

    assert result.saved_tickers == ("GOOD",)
    assert result.failures == (
        AnalysisSnapshotFailure(
            "MISSING",
            "Faltan resultados del análisis: fundamental_results, "
            "valuation_results, relative_results, risk_results, opportunity_results",
        ),
    )
    assert [snapshot["ticker"] for snapshot in journal.snapshots] == ["GOOD"]


def test_versioned_policy_is_saved_once_without_overwriting_legacy(monkeypatch) -> None:
    journal = FakeJournal([{"ticker": "AAA", "analyzed_at": "2026-09-26", "note": "Nota antigua"}])
    monkeypatch.setattr("src.analysis_snapshots.evaluate_latest_signal",
                        lambda frame, config, *, ticker: _signal(ticker))
    policy = build_policy_snapshot({"Ticker": "AAA", "Fecha": "2026-09-26"}, config={"risk_pct": 0.5})
    args = (journal, ["AAA"], _prepared("AAA"), *_results("AAA"), StrategyConfig())
    first = persist_analysis_batch_snapshots(*args, policy_snapshots={"AAA": policy})
    second = persist_analysis_batch_snapshots(*args, policy_snapshots={"AAA": policy})
    assert first.saved_tickers == ("AAA",)
    assert second.existing_tickers == ("AAA",)
    assert len(journal.snapshots) == 2
    assert journal.snapshots[0]["note"] == "Nota antigua"
    assert decode_policy_note(journal.snapshots[1]["note"]) == policy
    changed = build_policy_snapshot({"Ticker": "AAA", "Fecha": "2026-09-26"}, config={"risk_pct": 0.25})
    third = persist_analysis_batch_snapshots(*args, policy_snapshots={"AAA": changed})
    assert third.saved_tickers == ("AAA",)
    assert len(journal.snapshots) == 3


def test_storage_preserves_complete_policy_and_long_plan(tmp_path) -> None:
    from src.journal import TradingJournal, normalize_analysis_snapshot, normalize_portfolio_snapshot_position
    from src.portfolio_thesis import POSITION_PLAN_MARKER
    import json
    policy = build_policy_snapshot({"Ticker": "AAA"}, position_context={"thesis": "tesis " * 350}, held=True)
    note = encode_policy_note("Análisis versionado", policy)
    normalized = normalize_analysis_snapshot(ticker="AAA", analyzed_at="2026-09-26", price=120,
        opportunity_score=60, company_score=80, entry_score=70, valuation_score=65,
        relative_score=60, risk_score=70, opportunity_label="Revisar", entry_label="Vigilar",
        position_label="Mantener", note=note)
    assert len(normalized["note"]) > 1000
    assert decode_policy_note(normalized["note"]) == policy
    journal = TradingJournal(tmp_path / "policies.db", owner="test-policy")
    journal.add_analysis_snapshot(**{key: value for key, value in normalized.items() if key != "created_at"})
    loaded = journal.list_analysis_snapshots()
    assert decode_policy_note(loaded.iloc[0]["note"]) == policy
    comments = "Comentario anterior\n" + POSITION_PLAN_MARKER + " " + json.dumps({
        "strategy": "Crecimiento y momentum", "horizon": "Medio plazo", "thesis": "tesis " * 350})
    position = normalize_portfolio_snapshot_position(snapshot_date="2026-09-26", platform="Broker",
        asset_name="Empresa", analysis_ticker="AAA", value_eur=100, comments=comments)
    assert position["comments"] == comments
