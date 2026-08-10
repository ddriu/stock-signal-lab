from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pandas as pd

from config import StrategyConfig
from src.entry_opportunity import (
    STATUS_EVENT,
    calculate_entry_zones,
    combine_entry_opportunity_score,
    evaluate_entry_opportunity,
    evaluate_entry_timing,
    evaluate_event_risk,
    non_linking_ticker_text,
    sector_concentrations,
    ticker_display_html,
)
from src.indicators import add_indicators
from src.signal_engine import add_signal_columns, evaluate_latest_signal


def _prepared_frame() -> pd.DataFrame:
    index = pd.date_range("2025-01-02", periods=280, freq="B")
    trend = np.linspace(80.0, 120.0, len(index))
    wave = np.sin(np.linspace(0, 12, len(index))) * 1.5
    close = trend + wave
    raw = pd.DataFrame(
        {
            "open": close * 0.998,
            "high": close * 1.012,
            "low": close * 0.988,
            "close": close,
            "volume": np.linspace(900_000, 1_300_000, len(index)),
        },
        index=index,
    )
    config = StrategyConfig()
    return add_signal_columns(add_indicators(raw, config), config)


def _opportunity(ticker: str = "TEST", sector: str = "Technology"):
    frame = _prepared_frame()
    signal = evaluate_latest_signal(frame, StrategyConfig(), ticker=ticker)
    return evaluate_entry_opportunity(
        ticker=ticker,
        company_name=f"{ticker} Company",
        frame=frame,
        signal=signal,
        fundamental_score=78,
        fundamental_coverage=80,
        valuation_score=62,
        valuation_coverage=70,
        relative_score=72,
        relative_coverage=80,
        risk_score=65,
        risk_coverage=100,
        info={},
        sector=sector,
        market="United States",
    )


def test_gap_from_signal_penalizes_late_entry() -> None:
    frame = _prepared_frame()
    current = float(frame["close"].iloc[-1])
    near = evaluate_entry_timing("TEST", frame, signal_price=current)
    chased = evaluate_entry_timing("TEST", frame, signal_price=current / 1.25)

    assert chased.gap_from_signal_pct > 20
    assert chased.score < near.score


def test_earnings_penalty_and_same_day_block() -> None:
    reference = date(2026, 8, 10)
    near = evaluate_event_risk(
        {"earningsDate": (reference + timedelta(days=2)).isoformat()},
        reference_date=reference,
    )
    today = evaluate_event_risk(
        {"earningsDate": reference.isoformat()},
        reference_date=reference,
    )

    assert near.penalty > 0
    assert near.blocked is False
    assert today.blocked is True

    frame = _prepared_frame()
    normal = evaluate_entry_timing("TEST", frame)
    penalized = evaluate_entry_timing("TEST", frame, event=near)
    assert penalized.score == max(0, normal.score - near.penalty)


def test_entry_zones_are_ordered_and_use_structural_stop() -> None:
    zones = calculate_entry_zones(_prepared_frame())

    assert zones.aggressive_entry.lower <= zones.aggressive_entry.upper
    assert zones.preferred_entry.lower <= zones.preferred_entry.upper
    assert zones.excellent_entry.lower <= zones.excellent_entry.upper
    assert zones.invalidation < zones.current_price
    assert zones.invalidation < zones.excellent_entry.lower
    assert zones.risk_to_stop_pct is not None
    assert zones.risk_to_stop_pct > 0
    assert "ATR" in zones.basis


def test_dotted_ticker_is_text_not_external_url() -> None:
    visible = non_linking_ticker_text("1801.HK")
    rendered = ticker_display_html("HO.PA")

    assert visible.replace("\u2060", "") == "1801.HK"
    assert "href" not in rendered
    assert "http" not in rendered


def test_final_score_redistributes_missing_fundamentals() -> None:
    score, confidence = combine_entry_opportunity_score(
        technical_score=80,
        timing_score=70,
        fundamental_score=None,
        valuation_score=None,
        relative_score=60,
        relative_coverage=80,
        risk_score=70,
        risk_coverage=100,
    )

    assert 0 <= score <= 100
    assert score == 72
    assert confidence < 100


def test_same_day_event_controls_status_without_erasing_technical_score() -> None:
    frame = _prepared_frame()
    signal = evaluate_latest_signal(frame, StrategyConfig(), ticker="TEST")
    event_date = pd.Timestamp(frame.index[-1]).date().isoformat()
    result = evaluate_entry_opportunity(
        ticker="TEST",
        company_name="Test Company",
        frame=frame,
        signal=signal,
        fundamental_score=None,
        fundamental_coverage=0,
        valuation_score=None,
        valuation_coverage=0,
        relative_score=None,
        relative_coverage=0,
        risk_score=None,
        risk_coverage=0,
        info={"earningsDate": event_date},
    )

    assert result.status_code == STATUS_EVENT
    assert result.technical_score == signal.score


def test_sector_concentration_keeps_related_signals_together() -> None:
    first = _opportunity("BA.L", "Aerospace & Defense")
    second = replace(first, ticker="RTX", company_name="RTX")
    unrelated = replace(first, ticker="MA", sector="Financial Services")

    grouped = sector_concentrations([first, second, unrelated])

    assert grouped == {"Aerospace & Defense": ("BA.L", "RTX")}
