"""Deterministic entry context only: no network, DB, mail or simulated fills."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from src.data_sources import FxSnapshot
from src.growth_execution_review import ExecutionReviewConfig, review_growth_execution
from src.price_units import normalize_price_frame_units


DAY = date(2026, 10, 29)
CLOCK = "2026-10-29T22:30:00Z"


def frame(kind="breakout", scale=1.):
    index = pd.bdate_range(end=DAY, periods=40)
    output = pd.DataFrame({"open": 99.5, "high": 101., "low": 99., "close": 100.,
                           "volume": 100_000.}, index=index)
    if kind == "breakout":
        output.iloc[-1] = [100., 101.2, 99.8, 101., 200_000.]
    elif kind == "retest":
        output.iloc[-3] = [100., 101.2, 99.8, 101., 200_000.]
        output.iloc[-2] = [101., 101.3, 100.8, 101.1, 80_000.]
        output.iloc[-1] = [99.8, 100.6, 99.8, 100.5, 80_000.]
    output[["open", "high", "low", "close"]] *= scale
    return output


def result(**updates):
    return SimpleNamespace(**{"ticker": "TEST", "score": 88, "label": "Entrada fuerte", "as_of": DAY,
                              **updates})


def review(data=None, *, currency="EUR", fx=None, raw=None, **kwargs):
    if fx is not None and "fx_available_at" not in kwargs:
        kwargs["fx_available_at"] = "2026-10-29T15:30:00Z"
    return review_growth_execution(raw or result(), {"symbol": "TEST", "currency": currency},
        data if data is not None else frame(), fx, as_of=DAY, known_at=CLOCK, **kwargs)


def test_causal_breakout_preserves_inputs_raw_label_and_has_no_fill_or_stop_claim():
    data, raw = frame(), result()
    before = data.copy(deep=True)
    reading = review(data, raw=raw)
    pd.testing.assert_frame_equal(data, before)
    assert vars(raw) == vars(result())
    assert reading["eligible_baseline"] and reading["entry_setup_eligible"] and reading["eligible"]
    assert reading["setup"] == "breakout" and reading["raw_score"] == 88
    assert reading["prior_level"] == 100 and reading["extension_atr"] == pytest.approx(.5)
    assert reading["volume_ratio_prior"] == 2
    assert "fills" not in reading and "stop_price" not in reading and "trade" not in reading
    assert any("orden intradía" in value for value in reading["limitations"])


def test_retest_uses_a_previously_known_breakout_not_future_pivots():
    reading = review(frame("retest"))
    assert reading["setup"] == "retest" and reading["entry_setup_eligible"]
    assert reading["prior_level"] == 100
    assert reading["extension_atr"] == pytest.approx((100.5 - 100) / reading["atr_prior"])
    assert reading["atr_reference_session"] < reading["session"]


def test_appending_future_bars_even_with_invalid_prices_cannot_change_past_review():
    data = frame()
    future = pd.DataFrame({"open": [-1.], "high": [-2.], "low": [1000.], "close": [500.],
                           "volume": [-1.]}, index=[pd.Timestamp("2026-10-30")])
    assert review(data) == review(pd.concat([data, future]))


def test_intraday_current_bar_is_excluded_even_when_asof_is_today():
    data = pd.concat([frame(), pd.DataFrame({"open": [101.], "high": [300.], "low": [100.],
        "close": [250.], "volume": [1_000_000.]}, index=[pd.Timestamp("2026-10-30")])])
    reading = review_growth_execution(result(), {"currency": "EUR"}, data, None,
        as_of="2026-10-30", known_at="2026-10-30T15:00:00Z")
    assert reading["session"] == DAY.isoformat() and reading["reference_close"] == 101
    assert reading["entry_setup_eligible"]


@pytest.mark.parametrize("currency,scale,rate", [("EUR", 1., 1.), ("USD", 1.2, 1.2), ("JPY", 160., 160.)])
def test_economically_identical_prices_have_identical_eur_adv_and_setup(currency, scale, rate):
    fx = FxSnapshot(DAY, {"EUR": 1., currency: rate})
    expected = review()
    actual = review(frame(scale=scale), currency=currency, fx=fx)
    assert actual["adv_eur"] == pytest.approx(expected["adv_eur"])
    assert actual["entry_setup_eligible"] == expected["entry_setup_eligible"]
    assert actual["extension_atr"] == pytest.approx(expected["extension_atr"])


def test_raw_pence_and_already_normalized_pounds_are_not_double_scaled():
    raw = frame(scale=100.)
    fx = FxSnapshot(DAY, {"EUR": 1., "GBP": .8})
    reading = review(raw, currency="GBp", fx=fx)
    normalized = normalize_price_frame_units(raw, "GBp")
    again = review(normalized, currency="GBp", fx=fx)
    pounds = review(frame(), currency="GBP", fx=fx)
    assert reading["currency"] == "GBP"
    assert reading["reference_close"] == 101
    assert reading["adv_eur"] == pytest.approx(pounds["adv_eur"])
    assert again == reading


@pytest.mark.parametrize("fx", [None, FxSnapshot(None, {"EUR": 1., "USD": 1.2}),
    FxSnapshot(date(2026, 10, 30), {"EUR": 1., "USD": 1.2}),
    FxSnapshot(date(2026, 10, 20), {"EUR": 1., "USD": 1.2}),
    FxSnapshot(DAY, {"EUR": 1., "USD": 0.}), FxSnapshot(DAY, {"EUR": 1.})])
def test_unknown_future_old_or_invalid_fx_is_nd_and_never_changes_baseline(fx):
    reading = review(currency="USD", fx=fx)
    assert reading["status"] == "blocked_data" and reading["adv_eur"] is None
    assert reading["eligible_baseline"] and not reading["entry_setup_eligible"]
    assert reading["raw_score"] == 88 and reading["raw_label"] == "Entrada fuerte"


@pytest.mark.parametrize("column,value", [("high", 1.), ("low", 500.), ("open", 0.),
    ("volume", -1.), ("volume", float("nan")), ("close", float("inf"))])
def test_invalid_ohlcv_blocks_execution_context(column, value):
    data = frame()
    data.loc[data.index[-1], column] = value
    reading = review(data)
    assert reading["status"] == "blocked_data" and not reading["entry_setup_eligible"]
    assert reading["eligible_baseline"]


def test_incompatible_currency_declarations_are_not_silently_overridden():
    data = frame()
    data.attrs["quote_currency"] = "USD"
    reading = review(data, currency="EUR")
    assert reading["status"] == "blocked_data" and reading["adv_eur"] is None


def test_illiquid_context_does_not_downgrade_original_model_score():
    data = frame()
    data.volume *= .001
    reading = review(data)
    assert reading["status"] == "illiquid" and reading["eligible_baseline"]
    assert not reading["entry_setup_eligible"] and reading["raw_score"] == 88


def test_a_candle_does_not_create_an_entry_if_the_original_model_has_none():
    reading = review(raw=result(label="No preparada", score=40))
    assert reading["setup"] == "breakout" and not reading["eligible_baseline"]
    assert not reading["entry_setup_eligible"] and reading["raw_score"] == 40


def test_excess_extension_and_unconfirmed_volume_are_distinct_from_bad_data():
    data = frame()
    data.iloc[-1] = [100., 110.2, 99.8, 110., 200_000.]
    extended = review(data)
    assert extended["setup"] == "breakout" and extended["status"] == "watch"
    data = frame()
    data.loc[data.index[-1], "volume"] = 100_000.
    no_volume = review(data)
    assert no_volume["setup"] == "breakout" and not no_volume["entry_setup_eligible"]


@pytest.mark.parametrize("kind", ["missing_open", "duplicate", "unsorted", "short"])
def test_missing_ambiguous_or_insufficient_daily_history_is_blocked(kind):
    data = frame()
    if kind == "missing_open": data = data.drop(columns="open")
    if kind == "duplicate": data = pd.concat([data, data.tail(1)])
    if kind == "unsorted": data = data.iloc[::-1]
    if kind == "short": data = data.tail(10)
    assert review(data)["status"] == "blocked_data"


def test_signal_and_bar_dates_cannot_be_mixed():
    reading = review(raw=result(as_of="2026-10-28"))
    assert reading["status"] == "blocked_data" and reading["eligible_baseline"]


def test_mapping_result_and_fixed_preset_are_supported_without_requiring_extra_strategies():
    reading = review(raw=vars(result()), config=ExecutionReviewConfig())
    assert reading["entry_setup_eligible"]
    assert reading["version"] == "growth_execution_review_v1"


def test_atr_is_never_filled_when_all_prior_ranges_are_zero():
    data = frame("none")
    data[["open", "high", "low", "close"]] = 100.
    assert review(data)["status"] == "blocked_data"


def test_invalid_boolean_or_complex_prices_are_not_silently_coerced():
    for value in (True, 100 + 2j):
        data = frame().astype(object)
        data.loc[data.index[-1], "close"] = value
        assert review(data)["status"] == "blocked_data"


def test_naive_clock_and_unrecognized_calendar_are_not_certified():
    reading = review_growth_execution(result(), {"currency": "EUR"}, frame(), None,
        as_of=DAY, known_at="2026-10-29T22:30:00")
    assert reading["status"] == "blocked_data"
    reading = review_growth_execution(result(ticker="TEST.ZZ"), {"currency": "EUR"}, frame(), None,
        as_of=DAY, known_at=CLOCK)
    assert reading["status"] == "blocked_data"


def test_gap_and_two_touched_levels_do_not_create_intrabar_fills_or_pnl():
    data = frame()
    data.iloc[-1] = [100., 110., 90., 101., 200_000.]
    reading = review(data)
    assert "fills" not in reading and "pnl" not in reading and "exit" not in reading
    assert any("orden intradía" in text for text in reading["limitations"])


@pytest.mark.parametrize("config", [ExecutionReviewConfig(adv_sessions=2.5),
    ExecutionReviewConfig(adv_sessions=True), ExecutionReviewConfig(min_turnover_eur=float("nan"))])
def test_invalid_experiment_configuration_is_explicitly_rejected(config):
    with pytest.raises(ValueError): review(config=config)


def test_same_day_fx_without_known_publication_time_is_not_certified():
    fx = FxSnapshot(DAY, {"EUR": 1., "USD": 1.2})
    reading = review(currency="USD", fx=fx, fx_available_at=None)
    assert reading["status"] == "blocked_data" and reading["adv_eur"] is None


def test_prior_day_fx_can_be_used_conservatively_without_publication_timestamp():
    fx = FxSnapshot(date(2026, 10, 28), {"EUR": 1., "USD": 1.2})
    assert review(currency="USD", fx=fx, fx_available_at=None)["entry_setup_eligible"]


def test_same_day_ecb_fx_is_not_known_at_korean_morning_close():
    data = frame()
    raw = result(ticker="000660.KS")
    fx = FxSnapshot(DAY, {"EUR": 1., "KRW": 1600.})
    for availability in (None, "2026-10-29T15:30:00Z"):
        reading = review_growth_execution(raw, {"currency": "KRW"}, data, fx,
            as_of=DAY, known_at="2026-10-29T07:00:00Z", fx_available_at=availability)
        assert reading["status"] == "blocked_data" and reading["adv_eur"] is None


@pytest.mark.parametrize("availability", ["2026-10-29T23:00:00Z", "2026-10-29T15:30:00", "2026-10-28T23:00:00Z"])
def test_fx_availability_must_be_aware_known_and_not_before_economic_day(availability):
    fx = FxSnapshot(DAY, {"EUR": 1., "USD": 1.2})
    reading = review(currency="USD", fx=fx, fx_available_at=availability)
    assert reading["status"] == "blocked_data" and reading["adv_eur"] is None


def test_explicit_bar_arrival_after_the_signal_cannot_leak_into_execution_context():
    data = frame()
    data["close_available_at"] = pd.to_datetime(data.index, utc=True) + pd.Timedelta(hours=21)
    assert review(data)["entry_setup_eligible"]
    data.loc[data.index[-1], "close_available_at"] = pd.Timestamp("2026-10-30T01:00:00Z")
    reading = review(data)
    assert reading["status"] == "blocked_data" and reading["adv_eur"] is None
    assert reading["eligible_baseline"]


def test_explicit_intraday_bar_arrival_does_not_certify_a_completed_daily_bar():
    data = frame()
    data["close_available_at"] = pd.to_datetime(data.index, utc=True) + pd.Timedelta(hours=21)
    data.loc[data.index[-1], "close_available_at"] = pd.Timestamp("2026-10-29T15:00:00Z")
    assert review(data)["status"] == "blocked_data"


def test_missing_source_arrival_clock_is_not_replaced_by_calendar_close():
    data = frame()
    data["source_close_available_at"] = pd.to_datetime(data.index, utc=True) + pd.Timedelta(hours=21)
    data.loc[data.index[-1], "source_close_available_at"] = pd.NaT
    assert review(data)["status"] == "blocked_data"
