from __future__ import annotations

import numpy as np
import pandas as pd

from config import StrategyConfig
from src.fundamentals import evaluate_fundamentals
from src.indicators import add_indicators
from src.market_analysis import build_instrument_report, detect_support_resistance
from src.opportunity import evaluate_relative_strength, evaluate_risk, evaluate_valuation
from src.price_units import normalize_price_frame_units
from src.signal_engine import add_signal_columns, evaluate_latest_signal


def _turnaround_frame() -> pd.DataFrame:
    index = pd.date_range("2025-06-02", periods=300, freq="B")
    first = np.linspace(26.0, 14.0, 210)
    recovery = np.linspace(14.0, 19.0, 90) + np.sin(np.linspace(0, 10, 90)) * 0.45
    close = np.concatenate([first, recovery])
    raw = pd.DataFrame(
        {
            "open": close * 0.997,
            "high": close * 1.018,
            "low": close * 0.982,
            "close": close,
            "volume": np.linspace(3_000_000, 6_000_000, len(index)),
        },
        index=index,
    )
    raw.attrs["display_currency"] = "GBP"
    return add_signal_columns(add_indicators(raw, StrategyConfig()), StrategyConfig())


def _info() -> dict[str, object]:
    return {
        "symbol": "TEST.L",
        "currency": "GBP",
        "sector": "Consumer Defensive",
        "returnOnEquity": 0.15,
        "profitMargins": 0.09,
        "operatingMargins": 0.31,
        "revenueGrowth": None,
        "earningsGrowth": float("nan"),
        "debtToEquity": 172.0,
        "currentRatio": 1.6,
        "freeCashflow": 2_480_000_000,
        "marketCap": 39_850_000_000,
        "forwardPE": 14.35,
        "trailingPE": 31.03,
        "pegRatio": 0.93,
        "priceToBook": 4.94,
        "earningsTimestamp": 1_775_433_600,
        "exDividendDate": 1_797_292_800,
    }


def _report():
    frame = _turnaround_frame()
    info = _info()
    fundamentals = evaluate_fundamentals(info, "TEST.L")
    valuation = evaluate_valuation(info, "TEST.L")
    relative = evaluate_relative_strength(
        "TEST.L", frame, None, broad_name="^FTSE"
    )
    risk = evaluate_risk("TEST.L", frame)
    signal = evaluate_latest_signal(frame, StrategyConfig(), ticker="TEST.L")
    return build_instrument_report(
        ticker="TEST.L",
        frame=frame,
        info=info,
        signal=signal,
        fundamentals=fundamentals,
        valuation=valuation,
        relative=relative,
        risk=risk,
    )


def test_report_keeps_six_scores_separate_and_fixed_stop_in_production() -> None:
    report = _report()

    assert report.entry_score.score is not None
    assert report.position_score.score is not None
    assert report.momentum_score.score is not None
    assert report.trend_score.score is not None
    assert report.risk_score.score is not None
    assert report.quality_score.score is not None
    assert report.fixed_stop.stop_method == "fixed"
    assert report.fixed_stop.recommended_stop == round(report.price * 0.92, 6)
    assert report.structural_stop.stop_method == "structural"


def test_levels_and_targets_come_only_from_observed_prices_or_indicators() -> None:
    report = _report()

    assert len(report.supports) <= 3
    assert len(report.resistances) <= 3
    assert all(level.price < report.price for level in report.supports)
    assert all(level.price > report.price for level in report.resistances)
    assert all(target.price > report.price for target in report.targets)
    assert all(target.basis for target in report.targets)
    assert all(
        current.price < following.price
        for current, following in zip(report.targets, report.targets[1:])
    )


def test_entry_options_are_distinct_during_an_extended_move() -> None:
    report = _report()

    assert len(report.entries) == 3
    zones = {(entry.lower, entry.upper) for entry in report.entries}
    assert len(zones) == 3


def test_missing_fundamental_values_are_not_converted_to_zero() -> None:
    report = _report()

    assert report.quality_score.coverage_pct < 100
    assert all("0.0%" not in factor for factor in report.quality_score.risk_factors)


def test_pence_normalization_scales_all_price_dependent_levels() -> None:
    frame = _turnaround_frame().loc[:, ["open", "high", "low", "close", "volume"]] * 100
    frame["volume"] /= 100
    normalized = normalize_price_frame_units(frame, "GBp")
    prepared = add_signal_columns(add_indicators(normalized, StrategyConfig()), StrategyConfig())
    supports, resistances = detect_support_resistance(prepared)

    assert prepared["close"].iloc[-1] < 100
    assert all(level.price < 100 for level in (*supports, *resistances))
