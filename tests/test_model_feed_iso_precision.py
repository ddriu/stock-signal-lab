"""Real ledger clocks may preserve seconds or fractional seconds; same UTC."""
import pandas as pd
import pytest

from src.model_results_tracking import _reconstruct_feed


def test_signal_feed_accepts_mixed_iso_precision_without_changing_knowledge_clocks():
    clocks = ["2026-10-30T22:01:00+00:00", "2026-10-31T09:01:27.229603+00:00"]
    events = [{"event_type": "observation", "payload": {"feed_kind": "signal", "row": {
        "ticker": "SYNTH", "observed_at": clock, "event_id": str(i)}}}
        for i, clock in enumerate(clocks)]
    signals, bars, orders = _reconstruct_feed(events)
    assert signals.observed_at.tolist() == [pd.Timestamp(clock) for clock in clocks]
    assert bars == orders == {}


def test_price_feed_accepts_mixed_precision_for_all_availability_columns():
    clocks = ["2026-10-29T20:00:00Z", "2026-10-30T20:00:00.123456+00:00"]
    columns = ("open_at", "close_at", "close_available_at", "source_close_available_at")
    events = [{"event_type": "price_mark", "payload": {"feed_kind": "bar", "row": {
        "ticker": "SYNTH", "session": day, "currency": "USD", **dict.fromkeys(columns, clock)}}}
        for day, clock in zip(("2026-10-29", "2026-10-30"), clocks)]
    _, bars, _ = _reconstruct_feed(events)
    for column in columns:
        assert bars["SYNTH"][column].tolist() == [pd.Timestamp(clock) for clock in clocks]


def test_invalid_clock_still_fails_closed():
    with pytest.raises(ValueError):
        _reconstruct_feed([{"event_type": "observation", "payload": {
            "feed_kind": "signal", "row": {"observed_at": "not-a-clock"}}}])
