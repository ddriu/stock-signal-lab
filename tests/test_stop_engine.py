import numpy as np
import pandas as pd
import pytest

from src.stop_engine import (
    AtrMethod,
    StopConfig,
    StopMethod,
    analyze_initial_stop,
    calculate_atr,
    detect_support_levels,
    true_range,
)


def _price_history(periods: int = 50) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=periods, freq="B")
    close = np.linspace(100.0, 109.0, periods)
    low = close - 1.0
    low[-10] = 95.0
    return pd.DataFrame(
        {
            "open": close - 0.2,
            "high": close + 1.0,
            "low": low,
            "close": close,
            "volume": 1_000_000,
        },
        index=index,
    )


def test_atr_sma_matches_existing_rolling_true_range() -> None:
    frame = _price_history(30)
    expected = true_range(frame).rolling(14, min_periods=14).mean()
    actual = calculate_atr(frame, period=14, method=AtrMethod.SMA)
    pd.testing.assert_series_equal(actual, expected.rename("atr_14_sma"))


def test_atr_wilder_is_available_without_changing_sma() -> None:
    frame = _price_history(30)
    sma = calculate_atr(frame, period=14, method="sma")
    wilder = calculate_atr(frame, period=14, method="wilder")
    assert np.isfinite(wilder.iloc[-1])
    assert not np.isclose(sma.iloc[-1], wilder.iloc[-1])


def test_fixed_remains_default_and_keeps_exact_percentage() -> None:
    analysis = analyze_initial_stop(
        _price_history(),
        entry_price=110.0,
        config=StopConfig(fixed_stop_pct=8.0),
    )
    assert analysis.stop_method == StopMethod.FIXED.value
    assert analysis.recommended_stop == pytest.approx(101.2)
    assert analysis.stop_distance_pct == pytest.approx(8.0)


def test_structural_stop_uses_causal_support_and_atr_buffer() -> None:
    frame = _price_history()
    analysis = analyze_initial_stop(
        frame,
        entry_price=110.0,
        config=StopConfig(
            method=StopMethod.STRUCTURAL,
            atr_multiplier=2.0,
            support_buffer_atr=0.5,
            max_stop_pct=25.0,
        ),
    )
    assert analysis.support is not None
    assert analysis.support_age_bars is not None
    assert analysis.structural_stop <= analysis.atr_stop
    assert analysis.structural_stop <= analysis.support - analysis.support_buffer + 1e-6
    assert analysis.recommended_stop == analysis.structural_stop


def test_future_bars_never_change_stop_at_decision_date() -> None:
    history = _price_history()
    decision = history.index[-1]
    future_index = pd.date_range(decision + pd.offsets.BDay(1), periods=8, freq="B")
    future = pd.DataFrame(
        {
            "open": 70.0,
            "high": 72.0,
            "low": 50.0,
            "close": 55.0,
            "volume": 9_000_000,
        },
        index=future_index,
    )
    config = StopConfig(method="structural", atr_multiplier=2.5)
    before = analyze_initial_stop(history, entry_price=110.0, config=config, decision_at=decision)
    after = analyze_initial_stop(
        pd.concat([history, future]),
        entry_price=110.0,
        config=config,
        decision_at=decision,
    )
    assert before == after
    levels = detect_support_levels(pd.concat([history, future]).loc[:decision], entry_price=110.0)
    assert all(level.as_of <= decision for level in levels)


@pytest.mark.parametrize("method", ["fixed", "atr", "structural"])
def test_method_selection_returns_its_candidate(method: str) -> None:
    analysis = analyze_initial_stop(
        _price_history(),
        entry_price=110.0,
        config=StopConfig(method=method),
    )
    candidate = {
        "fixed": analysis.fixed_stop,
        "atr": analysis.atr_stop,
        "structural": analysis.structural_stop,
    }[method]
    assert analysis.recommended_stop == candidate
