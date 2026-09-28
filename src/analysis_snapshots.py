"""Persistencia idempotente de análisis completos calculados por lotes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
import math
from typing import Protocol, TypeVar

import pandas as pd

from config import StrategyConfig
from src.fundamentals import FundamentalResult
from src.opportunity import (
    OpportunityResult,
    RelativeStrengthResult,
    RiskResult,
    ValuationResult,
)
from src.signal_engine import evaluate_latest_signal


DEFAULT_BATCH_SNAPSHOT_NOTE = "Seguimiento automático del análisis por lote"


class AnalysisSnapshotJournal(Protocol):
    """Contrato mínimo compartido por los diarios SQLite y Supabase."""

    def list_analysis_snapshots(self, ticker: str | None = None) -> pd.DataFrame: ...

    def add_analysis_snapshot(self, **values: object) -> int: ...


@dataclass(frozen=True)
class AnalysisSnapshotFailure:
    """Fallo aislado que no impide guardar el resto del lote."""

    ticker: str
    reason: str


@dataclass(frozen=True)
class BatchAnalysisSnapshotResult:
    """Resumen determinista de una operación de persistencia por lote."""

    saved_tickers: tuple[str, ...]
    existing_tickers: tuple[str, ...]
    failures: tuple[AnalysisSnapshotFailure, ...]

    @property
    def saved_count(self) -> int:
        return len(self.saved_tickers)


_Result = TypeVar("_Result")


def _results_by_ticker(values: Mapping[str, _Result]) -> dict[str, _Result]:
    return {
        str(ticker).strip().upper(): result
        for ticker, result in values.items()
        if str(ticker).strip()
    }


def _snapshot_date(value: object) -> date | None:
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(parsed):
        return None
    return parsed.date()


def _existing_snapshot_keys(snapshots: pd.DataFrame) -> set[tuple[str, date]]:
    if snapshots.empty or not {"ticker", "analyzed_at"}.issubset(snapshots.columns):
        return set()
    keys: set[tuple[str, date]] = set()
    for ticker, analyzed_at in zip(
        snapshots["ticker"], snapshots["analyzed_at"], strict=True
    ):
        normalized_ticker = str(ticker).strip().upper()
        analyzed_date = _snapshot_date(analyzed_at)
        if normalized_ticker and analyzed_date is not None:
            keys.add((normalized_ticker, analyzed_date))
    return keys


def _price_at_signal(frame: pd.DataFrame, analyzed_at: object) -> float:
    if frame.empty or "close" not in frame.columns:
        raise ValueError("El histórico preparado no contiene un cierre analizable.")
    matching = frame.loc[frame.index == pd.Timestamp(analyzed_at), "close"]
    if matching.empty:
        raise ValueError("La fecha de la señal no existe en el histórico preparado.")
    price = float(matching.iloc[-1])
    if not math.isfinite(price) or price <= 0:
        raise ValueError("El cierre de la señal debe ser un número positivo.")
    return price


def persist_analysis_batch_snapshots(
    journal: AnalysisSnapshotJournal,
    tickers: Iterable[str],
    prepared: Mapping[str, pd.DataFrame],
    fundamental_results: Mapping[str, FundamentalResult],
    valuation_results: Mapping[str, ValuationResult],
    relative_results: Mapping[str, RelativeStrengthResult],
    risk_results: Mapping[str, RiskResult],
    opportunity_results: Mapping[str, OpportunityResult],
    strategy: StrategyConfig,
    *,
    note: str = DEFAULT_BATCH_SNAPSHOT_NOTE,
) -> BatchAnalysisSnapshotResult:
    """Guarda como máximo una fotografía completa por ticker y sesión.

    La fecha relevante es la de la última señal válida, no la fecha en la que
    se ejecuta el proceso. El historial se consulta una sola vez para que los
    reruns de Streamlit y los lotes solapados sean idempotentes. Un fallo de un
    ticker se devuelve en el resultado y no cancela los demás.

    Si no puede leerse el historial, la excepción se propaga: escribir sin esa
    comprobación rompería la garantía de una sola fotografía diaria.
    """

    requested_tickers = tuple(
        dict.fromkeys(
            normalized
            for raw_ticker in tickers
            if (normalized := str(raw_ticker).strip().upper())
        )
    )
    prepared_by_ticker = _results_by_ticker(prepared)
    fundamentals_by_ticker = _results_by_ticker(fundamental_results)
    valuations_by_ticker = _results_by_ticker(valuation_results)
    relatives_by_ticker = _results_by_ticker(relative_results)
    risks_by_ticker = _results_by_ticker(risk_results)
    opportunities_by_ticker = _results_by_ticker(opportunity_results)

    existing_keys = _existing_snapshot_keys(journal.list_analysis_snapshots())
    saved: list[str] = []
    existing: list[str] = []
    failures: list[AnalysisSnapshotFailure] = []

    sources: tuple[tuple[str, Mapping[str, object]], ...] = (
        ("prepared", prepared_by_ticker),
        ("fundamental_results", fundamentals_by_ticker),
        ("valuation_results", valuations_by_ticker),
        ("relative_results", relatives_by_ticker),
        ("risk_results", risks_by_ticker),
        ("opportunity_results", opportunities_by_ticker),
    )

    for ticker in requested_tickers:
        missing = [name for name, values in sources if ticker not in values]
        if missing:
            failures.append(
                AnalysisSnapshotFailure(
                    ticker,
                    "Faltan resultados del análisis: " + ", ".join(missing),
                )
            )
            continue

        frame = prepared_by_ticker[ticker]
        fundamentals = fundamentals_by_ticker[ticker]
        valuation = valuations_by_ticker[ticker]
        relative = relatives_by_ticker[ticker]
        risk = risks_by_ticker[ticker]
        opportunity = opportunities_by_ticker[ticker]

        try:
            signal = evaluate_latest_signal(frame, strategy, ticker=ticker)
            analyzed_date = pd.Timestamp(signal.as_of).date()
            snapshot_key = (ticker, analyzed_date)
            if snapshot_key in existing_keys:
                existing.append(ticker)
                continue

            journal.add_analysis_snapshot(
                ticker=ticker,
                analyzed_at=signal.as_of,
                price=_price_at_signal(frame, signal.as_of),
                opportunity_score=opportunity.score,
                confidence_pct=opportunity.confidence_pct,
                company_score=fundamentals.score,
                entry_score=signal.score,
                valuation_score=valuation.score,
                relative_score=relative.score,
                risk_score=risk.score,
                opportunity_label=opportunity.label,
                entry_label=signal.label,
                position_label=signal.position_label,
                expected_return_pct=None,
                positive_rate_pct=None,
                expected_price=None,
                horizon_days=None,
                sector=fundamentals.sector or "",
                explanation=opportunity.explanation,
                note=note,
            )
        except Exception as exc:
            failures.append(
                AnalysisSnapshotFailure(ticker, str(exc) or type(exc).__name__)
            )
            continue

        existing_keys.add(snapshot_key)
        saved.append(ticker)

    return BatchAnalysisSnapshotResult(
        saved_tickers=tuple(saved),
        existing_tickers=tuple(existing),
        failures=tuple(failures),
    )
