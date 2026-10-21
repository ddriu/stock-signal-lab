"""Offline causal data contracts, with synthetic observations and schedules."""

from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from config import StrategyConfig
from src.analysis_policy import build_policy_snapshot, encode_policy_note
from src.indicators import add_indicators
from src.price_units import normalize_price_frame_units
from src.signal_engine import add_signal_columns
from src.strategy_audit_data import normalize_audit_signals, prepare_audit_bars


def _snapshot(**changes: object) -> dict[str, object]:
    return {
        "id": 10, "ticker": "OLD", "analyzed_at": "2026-10-15",
        "created_at": "2026-10-16T19:00:00+00:00", "entry_label": "Entrada fuerte",
        "entry_score": 80, "position_label": "Vender", "price": 100.0,
        "sector": "Technology", "company_score": 45, "valuation_score": 20,
        "opportunity_score": 99, "risk_score": 55, "note": "", **changes,
    }


def _growth_note(**primary_changes: object) -> str:
    return encode_policy_note("Original note", {
        "policy_version": "dual_strategy_v1", "ticker": "OLD", "price": 101.0,
        "currency": "USD", "config": {},
        "primary": {"strategy_id": "growth_momentum", "label": "Crecimiento + momentum",
                    "status": "Entrada candidata", "entry_reading": "Candidata a nueva entrada; confirmar precio y tamaño",
                    "score": 76, "data_status": "complete", "entry_candidate": True,
                    "as_of": "2026-10-15", **primary_changes},
        "comparative": {"quality_score": 7}, "position": {},
    })


def _schedule(index: pd.DatetimeIndex) -> pd.DataFrame:
    # Synthetic authoritative times, not a guessed real exchange calendar.
    return pd.DataFrame({
        "market_open": pd.to_datetime([f"{session.date()}T14:00:00Z" for session in index]),
        "market_close": pd.to_datetime([f"{session.date()}T20:00:00Z" for session in index]),
    }, index=index)


def _raw(periods: int = 230) -> pd.DataFrame:
    index = pd.bdate_range("2025-01-02", periods=periods)
    close = 100 + np.arange(periods) * .1
    frame = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                          "close": close, "volume": 10000.0}, index=index)
    frame.attrs["adjustment_basis"] = "split_adjusted"
    return frame


def _prepare(raw: pd.DataFrame, **changes: object):
    arguments = dict(ticker="TEST", currency="USD", schedule=_schedule(raw.index.unique()),
                     fx=None, config=StrategyConfig())
    arguments.update(changes)
    return prepare_audit_bars(raw, **arguments)


def test_created_at_is_the_observation_clock_not_analyzed_date_or_id() -> None:
    result = normalize_audit_signals([_snapshot()])
    row = result.signals.iloc[0]
    assert row["observed_at"] == pd.Timestamp("2026-10-16T19:00:00Z")
    assert row["signal_session"] == pd.Timestamp("2026-10-15")
    assert row["entry_label"] == "Entrada fuerte"
    assert row["entry_score"] == 80
    assert row["opportunity_score"] == 99
    assert row["company_score"] == 45
    assert "position_label" not in result.signals
    assert row["observed_price"] == 100
    assert row["reconstructed_price"] is None
    assert result.manifest["historical_position_labels_used"] is False
    assert "created_at_is_client_generated_not_independently_attested" in result.manifest["limitations"]
    assert "historical_strategy_config_version_not_recorded" in row["limitations"]


@pytest.mark.parametrize("created", [None, pd.NA, "", "not a timestamp", "2026-10-16T19:00:00"])
def test_missing_or_naive_creation_time_has_no_backdated_fallback(created: object) -> None:
    result = normalize_audit_signals([_snapshot(created_at=created)])
    assert result.signals.empty
    assert len(result.rejected) == 1
    assert "created_at" in result.rejected.iloc[0]["reason"]


def test_observation_offsets_are_converted_to_utc() -> None:
    result = normalize_audit_signals([_snapshot(created_at="2026-10-16T21:00:00+02:00")])
    assert result.signals.iloc[0]["observed_at"] == pd.Timestamp("2026-10-16T19:00:00Z")


def test_future_signal_session_is_rejected() -> None:
    result = normalize_audit_signals([_snapshot(analyzed_at="2026-10-17")])
    assert result.signals.empty
    assert result.rejected.iloc[0]["reason"] == "signal_session_after_observation"


def test_first_observation_wins_with_numeric_id_tiebreak_and_revisions_preserved() -> None:
    originals = [
        _snapshot(id=1, created_at="2026-10-17T19:00:00Z", entry_score=95),
        _snapshot(id=20, entry_score=85), _snapshot(id=2, entry_score=70),
    ]
    before = deepcopy(originals)
    result = normalize_audit_signals(originals)
    reordered = normalize_audit_signals(reversed(originals))
    assert result.signals.iloc[0]["id"] == 2
    assert result.signals.iloc[0]["entry_score"] == 70
    assert result.rejected["reason"].tolist() == ["later_revision", "later_revision"]
    assert {row["entry_score"] for row in result.rejected["raw_record"]} == {85, 95}
    pd.testing.assert_frame_equal(result.signals, reordered.signals)
    assert result.manifest["input_sha256"] == reordered.manifest["input_sha256"]
    assert originals == before


def test_all_historical_symbols_are_kept_without_current_favorites_filter() -> None:
    data = pd.DataFrame([_snapshot(ticker="DELISTED"), _snapshot(id=11, ticker="AMZ")])
    before = data.copy(deep=True)
    result = normalize_audit_signals(data)
    assert set(result.signals["ticker"]) == {"DELISTED", "AMZN"}
    assert result.coverage["universe"] == ["AMZN", "DELISTED"]
    pd.testing.assert_frame_equal(data, before)


def test_growth_requires_its_own_policy_and_never_falls_back_to_technical() -> None:
    result = normalize_audit_signals([_snapshot()], source="growth")
    assert result.signals.empty
    assert result.rejected.iloc[0]["reason"] == "missing_or_invalid_growth_policy"
    assert not normalize_audit_signals([_snapshot()], source="technical").signals.empty


def test_growth_source_keeps_scores_separate_and_preserves_observed_price() -> None:
    row = _snapshot(note=_growth_note())
    growth = normalize_audit_signals([row], source="growth").signals.iloc[0]
    technical = normalize_audit_signals([row], source="technical").signals.iloc[0]
    assert (growth["entry_label"], growth["entry_score"]) == ("Entrada candidata", 76)
    assert (growth["technical_entry_label"], growth["technical_entry_score"]) == ("Entrada fuerte", 80)
    assert growth["growth_score"] == 76
    assert growth["opportunity_score"] == 99
    assert growth["company_score"] == 45  # Not overwritten by comparative=7.
    assert growth["observed_price"] == 100
    assert growth["policy_price"] == 101
    assert technical["entry_score"] == 80
    assert growth["event_id"] != technical["event_id"]


@pytest.mark.parametrize("status", ["Entrada fuerte", "Entrada candidata", "No preparada", "Vigilancia activa"])
def test_actual_policy_builder_status_is_the_growth_reading_not_strategy_name(status: str) -> None:
    policy = build_policy_snapshot(
        {"Ticker": "OLD", "Fecha": "2026-10-15", "Momento entrada": 80},
        growth_result=SimpleNamespace(score=84, growth_score=88, momentum_score=82,
            context_score=70, confidence_pct=90, label=status, sector_key="technology",
            as_of=pd.Timestamp("2026-10-15")),
    )
    assert policy["primary"]["label"] == "Crecimiento + momentum"
    normalized = normalize_audit_signals([_snapshot(note=encode_policy_note("", policy))], source="growth")
    row = normalized.signals.iloc[0]
    assert (row["entry_label"], row["entry_score"]) == (status, 84)
    assert row["growth_strategy_label"] == "Crecimiento + momentum"
    assert row["growth_entry_reading"] == policy["primary"]["entry_reading"]
    assert "legacy_growth_label_field_used" not in row["limitations"]


def test_legacy_growth_label_fallback_is_whitelisted_and_explicit() -> None:
    valid = normalize_audit_signals([_snapshot(note=_growth_note(status=None, label="Entrada fuerte"))], source="growth")
    assert valid.signals.iloc[0]["entry_label"] == "Entrada fuerte"
    assert "legacy_growth_label_field_used" in valid.signals.iloc[0]["limitations"]
    invalid = normalize_audit_signals([_snapshot(note=_growth_note(status=None))], source="growth")
    assert invalid.signals.empty


def test_partial_growth_policy_cannot_masquerade_as_an_entry() -> None:
    result = normalize_audit_signals([
        _snapshot(note=_growth_note(data_status="partial", entry_candidate=False))
    ], source="growth")
    assert result.signals.empty
    assert result.rejected.iloc[0]["reason"] == "growth_entry_not_complete"


def test_policy_for_another_instrument_never_becomes_this_instruments_growth_signal() -> None:
    snapshot = _snapshot(ticker="OTHER", note=_growth_note())
    result = normalize_audit_signals([snapshot], source="growth")
    assert result.signals.empty
    assert result.rejected.iloc[0]["reason"] == "growth_policy_ticker_mismatch"
    technical = normalize_audit_signals([snapshot], source="technical").signals.iloc[0]
    assert technical["entry_score"] == 80
    assert technical["growth_score"] is None
    assert "unrelated_growth_policy_ignored" in technical["limitations"]


def test_empty_inputs_keep_typed_contract_and_invalid_source_is_rejected() -> None:
    result = normalize_audit_signals(pd.DataFrame())
    assert result.signals.empty and result.rejected.empty
    assert str(result.signals["observed_at"].dtype) == "datetime64[ns, UTC]"
    with pytest.raises(ValueError):
        normalize_audit_signals([], source="mixed")


def test_usd_bars_use_supplied_schedule_and_do_not_need_fx() -> None:
    raw = _raw()
    bars, issues = _prepare(raw)
    assert issues["code"].tolist() == ["exit_indicators_not_ready"]
    assert bars["fx_open"].eq(1).all() and bars["fx_close"].eq(1).all()
    assert bars["fx_open_age_days"].eq(0).all()
    assert bars.iloc[0]["open_at"] == _schedule(raw.index).iloc[0]["market_open"]
    assert bars.iloc[0]["close_at"] == _schedule(raw.index).iloc[0]["market_close"]
    assert {"reduce_setup", "sell_setup", "sma_long"}.issubset(bars.columns)
    assert bars.attrs["strategy_config"] == asdict(StrategyConfig())


def test_immature_exit_indicators_remain_unknown_and_cannot_certify_a_lot() -> None:
    from src.strategy_audit_engine import AuditConfig, run_strategy_audit

    raw = _raw(10)
    raw.attrs["original_provenance"] = {"provider": "synthetic", "immutable": True}
    bars, issues = _prepare(raw)
    assert bars["sma_long"].isna().all()
    assert not bars["exit_indicators_ready"].any()
    assert bars[["reduce_setup", "sell_setup"]].isna().all().all()
    assert "exit_indicators_not_ready" in set(issues["code"])
    assert bars.attrs["original_provenance"] == raw.attrs["original_provenance"]
    observed = _schedule(raw.index).iloc[0]["market_close"] + pd.Timedelta(minutes=1)
    signals = pd.DataFrame([dict(event_id="short-history", ticker="TEST",
        signal_session=raw.index[0].date().isoformat(), observed_at=observed,
        entry_label="Entrada fuerte", entry_score=80)])
    for exit_policy in ("reduce", "sell"):
        result = run_strategy_audit(signals, {"TEST": bars}, AuditConfig(
            start=raw.index[0].date(), end=raw.index[-1].date(),
            amount_usd=100, exit_policy=exit_policy))
        assert len(result.lots) == 1
        assert result.lots.iloc[0]["status"] == "unresolved"
        assert result.trades.empty  # Unknown is neither maintain nor sell.
        assert result.metrics["coverage_complete"] is False
        assert result.metrics["final_value_usd"] is None


def test_exit_readiness_requires_previous_confirmation_inputs_and_preserves_mature_rules() -> None:
    raw = _raw()
    config = StrategyConfig(trend_confirmation_days=3)
    bars, _ = _prepare(raw, config=config)
    expected = add_signal_columns(add_indicators(raw, config), config)
    assert bars["exit_indicators_ready"].iloc[:201].eq(False).all()
    assert bars["exit_indicators_ready"].iloc[201:].all()
    for flag in ("reduce_setup", "sell_setup"):
        assert bars[flag].iloc[:201].isna().all()
        pd.testing.assert_series_equal(bars[flag].iloc[201:].astype(bool),
            expected[flag].iloc[201:].rename_axis("session"), check_freq=False)
    assert asdict(config) == bars.attrs["strategy_config"]


def test_historical_close_clock_is_only_an_explicitly_declared_reconstruction_proxy() -> None:
    bars, _ = _prepare(_raw(3))
    assert bars["close_available_at"].equals(bars["close_at"])
    assert bars["source_close_available_at"].isna().all()
    assert bars.attrs["close_availability_assumed"] is True
    assert bars.attrs["close_availability_basis"] == "historical_exchange_close_proxy"
    assert "historical_close_availability_assumed" in bars.attrs["limitations"]


def test_explicit_close_availability_preserves_source_and_delays_dependent_bar_knowledge() -> None:
    raw = _raw(3)
    schedule = _schedule(raw.index)
    # The first final bar arrives after the next two. Indicators depending on
    # that bar were not knowable at those later sessions' exchange closes.
    delayed = schedule.iloc[-1]["market_close"] + pd.Timedelta(hours=1)
    raw["close_available_at"] = schedule["market_close"].copy()
    raw.loc[raw.index[0], "close_available_at"] = delayed
    before = raw.copy(deep=True)
    bars, issues = _prepare(raw)
    pd.testing.assert_series_equal(bars["source_close_available_at"],
        raw["close_available_at"].rename("source_close_available_at").rename_axis("session"), check_freq=False)
    assert bars["close_available_at"].eq(delayed).all()
    assert (bars["close_available_at"] >= bars["close_at"]).all()
    assert bars.attrs["close_availability_assumed"] is False
    assert "historical_close_availability_assumed" not in bars.attrs["limitations"]
    assert "missing_or_invalid_close_availability" not in set(issues["code"])
    prefix, _ = _prepare(raw.iloc[:2], schedule=schedule)
    pd.testing.assert_frame_equal(bars.iloc[:2], prefix)
    pd.testing.assert_frame_equal(raw, before)


@pytest.mark.parametrize("bad_clock", [None, "2025-01-02T19:59:59Z", "2025-01-02T21:00:00"])
def test_missing_naive_or_preclose_explicit_clock_never_falls_back_to_exchange_close(bad_clock: object) -> None:
    raw = _raw(3)
    raw["close_available_at"] = _schedule(raw.index)["market_close"].astype(object)
    raw.loc[raw.index[0], "close_available_at"] = bad_clock
    bars, issues = _prepare(raw)
    assert pd.isna(bars.iloc[0]["source_close_available_at"])
    assert bars["source_close_available_at"].iloc[1:].notna().all()
    assert bars["close_available_at"].isna().all()
    assert bars.attrs["close_availability_assumed"] is False
    assert "missing_or_invalid_close_availability" in set(issues["code"])


def test_identical_price_duplicate_uses_first_valid_arrival_and_repreparing_keeps_source_clocks() -> None:
    raw = _raw(3)
    raw["close_available_at"] = _schedule(raw.index)["market_close"] + pd.Timedelta(hours=2)
    duplicate = raw.iloc[[0]].copy()
    duplicate["close_available_at"] -= pd.Timedelta(hours=1)
    duplicated = pd.concat([raw, duplicate])
    duplicated.attrs = dict(raw.attrs)
    bars, _ = _prepare(duplicated)
    assert bars.iloc[0]["source_close_available_at"] == duplicate.iloc[0]["close_available_at"]
    repeated, _ = _prepare(bars)
    pd.testing.assert_frame_equal(bars, repeated)


def test_fx_is_asof_available_at_and_never_uses_later_close_for_open() -> None:
    raw = _raw(3)
    fx = pd.DataFrame({
        "close": [1.2, 1.4, 9.0],
        "available_at": [f"{raw.index[0].date()}T16:00:00Z",
                         f"{raw.index[1].date()}T14:00:00Z", "2030-01-01T00:00:00Z"],
    })
    before = fx.copy(deep=True)
    bars, issues = _prepare(raw, currency="EUR", fx=fx)
    assert pd.isna(bars.iloc[0]["fx_open"])
    assert bars.iloc[0]["fx_close"] == 1.2
    assert bars.iloc[1]["fx_open"] == 1.4  # Exact availability is allowed.
    assert bars.iloc[2]["fx_close"] == 1.4
    assert "fx_unavailable_at_some_bar_times" in set(issues["code"])
    assert (bars["fx_close_available_at"] <= bars["close_at"]).all()
    pd.testing.assert_frame_equal(fx, before)


@pytest.mark.parametrize("currency", ["EUR", "JPY", ""])
def test_unknown_or_missing_fx_never_assumes_usd(currency: str) -> None:
    bars, issues = _prepare(_raw(3), currency=currency)
    assert bars["fx_open"].isna().all() and bars["fx_close"].isna().all()
    assert not issues.empty


def test_naive_fx_availability_is_ignored_not_assumed_utc() -> None:
    raw = _raw(3)
    fx = pd.DataFrame({"close": [1.3], "available_at": ["2020-01-01T00:00:00"]})
    bars, issues = _prepare(raw, currency="GBP", fx=fx)
    assert bars["fx_open"].isna().all()
    assert "invalid_fx_observation" in set(issues["code"])


def test_years_old_fx_remains_evidence_but_cannot_execute_or_value_a_lot() -> None:
    from src.strategy_audit_engine import AuditConfig, run_strategy_audit

    raw = _raw(10)
    fx = pd.DataFrame({"close": [1.25], "available_at": ["2020-01-01T00:00:00Z"]})
    bars, issues = _prepare(raw, currency="GBP", fx=fx)
    assert bars[["fx_open", "fx_close"]].isna().all().all()
    assert bars["fx_open_available_at"].eq(pd.Timestamp("2020-01-01T00:00:00Z")).all()
    assert bars["fx_open_age_days"].gt(365).all()
    assert bars.attrs["fx_max_age_days"] == 7.0
    assert "stale_fx_observation" in set(issues["code"])
    assert "stale_fx_observation" in bars.attrs["limitations"]
    observed = _schedule(raw.index).iloc[0]["market_close"] + pd.Timedelta(minutes=1)
    signals = pd.DataFrame([dict(event_id="stale-fx", ticker="TEST",
        signal_session=raw.index[0].date().isoformat(), observed_at=observed,
        entry_label="Entrada fuerte", entry_score=80)])
    result = run_strategy_audit(signals, {"TEST": bars}, AuditConfig(
        start=raw.index[0].date(), end=raw.index[-1].date(), exit_policy="hold"))
    assert result.lots.empty
    assert result.metrics["unfilled_entries"] == 1
    assert result.metrics["coverage_complete"] is False


def test_fx_age_is_elapsed_calendar_time_including_weekends_and_limit_is_explicit() -> None:
    raw = _raw(3)
    opened = _schedule(raw.index).iloc[0]["market_open"]
    available = opened - pd.Timedelta(days=7)
    future = _schedule(raw.index).iloc[-1]["market_close"] + pd.Timedelta(hours=1)
    fx = pd.DataFrame({"close": [1.25, 99.0], "available_at": [available, future]})
    bars, _ = _prepare(raw, currency="GBP", fx=fx)
    assert bars.iloc[0]["fx_open"] == 1.25
    assert bars.iloc[0]["fx_open_age_days"] == 7.0
    assert pd.isna(bars.iloc[0]["fx_close"])
    assert bars.iloc[0]["fx_close_age_days"] == 7.25
    assert bars.iloc[-1]["fx_open_age_days"] == 11.0  # Thursday to Monday.
    assert pd.isna(bars.iloc[-1]["fx_close"])
    assert bars["fx_close_available_at"].eq(available).all()
    relaxed, _ = _prepare(raw, currency="GBP", fx=fx, fx_max_age_days=8)
    assert relaxed.iloc[0]["fx_close"] == 1.25
    assert relaxed.iloc[1]["fx_open"] == 1.25
    assert pd.isna(relaxed.iloc[1]["fx_close"])
    assert relaxed.attrs["fx_max_age_days"] == 8
    assert relaxed.attrs["strategy_config"] == bars.attrs["strategy_config"]


@pytest.mark.parametrize("maximum", [0, -1, float("nan"), float("inf"), True])
def test_fx_data_quality_limit_must_be_finite_and_positive(maximum: object) -> None:
    with pytest.raises(ValueError, match="fx_max_age_days"):
        _prepare(_raw(3), fx_max_age_days=maximum)


def test_gbp_normalizes_ohlc_once_before_indicators_and_keeps_volume() -> None:
    raw = _raw()
    raw.loc[:, ["open", "high", "low", "close"]] *= 100
    before = raw.copy(deep=True)
    fx = pd.DataFrame({"close": 1.25, "available_at": pd.to_datetime(raw.index, utc=True)})
    bars, _ = _prepare(raw, currency="GBp", fx=fx)
    already_normalized = normalize_price_frame_units(raw, "GBp")
    repeated, _ = _prepare(already_normalized, currency="GBp", fx=fx)
    assert bars.iloc[0]["close"] == 100
    assert bars.iloc[-1]["sma_long"] == pytest.approx(bars["close"].tail(200).mean())
    assert bars["volume"].tolist() == raw["volume"].tolist()
    assert bars.iloc[0]["fx_open"] == 1.25  # USD per GBP, not per penny.
    assert bars.attrs["display_currency"] == "GBP"
    pd.testing.assert_frame_equal(bars, repeated)
    pd.testing.assert_frame_equal(raw, before)


def test_cached_indicators_and_signal_flags_are_always_recomputed() -> None:
    raw = _raw()
    raw["sma_long"] = -999.0
    raw["signal_score"] = 0
    raw["sell_setup"] = True
    raw["reduce_setup"] = True
    config = StrategyConfig(trend_confirmation_days=3)
    bars, _ = _prepare(raw, config=config)
    expected = add_signal_columns(add_indicators(raw.loc[:, ["open", "high", "low", "close", "volume"]], config), config)
    expected.index.name = "session"
    mature = bars["exit_indicators_ready"]
    pd.testing.assert_series_equal(bars.loc[mature, "sell_setup"].astype(bool),
        expected.loc[mature, "sell_setup"], check_freq=False)
    assert bars["sma_long"].iloc[-1] > 0
    assert not bool(bars["sell_setup"].iloc[-1])


@pytest.mark.parametrize("factor", [.01, 100.0])
def test_atomic_unit_jump_quarantines_suffix_not_a_fake_real_loss(factor: float) -> None:
    raw = _raw()
    bad_session = raw.index[-2]
    raw.loc[bad_session:, ["open", "high", "low", "close"]] *= factor
    bars, issues = _prepare(raw)
    assert bars.index.max() < bad_session
    assert "possible_100x_unit_change" in set(issues["code"])
    assert bars.attrs["quarantined_from"] == bad_session.isoformat()
    prefix, _ = _prepare(raw.loc[:raw.index[-3]])
    pd.testing.assert_frame_equal(bars, prefix)


def test_discordant_duplicates_quarantine_but_identical_rows_deduplicate() -> None:
    raw = _raw()
    duplicate = raw.iloc[[-1]].copy()
    conflict = pd.concat([raw, duplicate.assign(close=float(duplicate["close"].iloc[0]) + .1)])
    conflict.attrs = dict(raw.attrs)
    bars, issues = _prepare(conflict)
    assert bars.index.max() == raw.index[-2]
    assert "discordant_duplicate_ohlcv" in set(issues["code"])
    identical = pd.concat([raw, duplicate])
    identical.attrs = dict(raw.attrs)
    bars, issues = _prepare(identical)
    assert len(bars) == len(raw)
    assert "identical_duplicate_ohlcv" in set(issues["code"])


@pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
def test_invalid_price_is_not_an_executable_stop_bar(bad: float) -> None:
    raw = _raw()
    raw.loc[raw.index[-1], "close"] = bad
    bars, issues = _prepare(raw)
    assert len(bars) == len(raw) - 1
    assert "invalid_ohlcv" in set(issues["code"])


def test_declared_adjusted_split_is_preserved_but_raw_split_blocks_from_event() -> None:
    raw = _raw()
    splits = pd.Series([2.0], index=[raw.index[-2]])
    adjusted, issues = _prepare(raw, splits=splits)
    assert len(adjusted) == len(raw)
    assert "split_adjusted_series_declared" in set(issues["code"])
    raw.attrs["adjustment_basis"] = "unadjusted"
    blocked, issues = _prepare(raw, splits=splits)
    assert blocked.index.max() == raw.index[-3]
    assert "unadjusted_split_not_supported" in set(issues["code"])


def test_missing_adjustment_metadata_is_an_explicit_limitation_not_invented() -> None:
    raw = _raw()
    raw.attrs.clear()
    bars, issues = _prepare(raw)
    assert len(bars) == len(raw)
    assert "adjustment_basis_not_recorded" in set(issues["code"])
    assert "adjustment_basis_not_recorded" in bars.attrs["limitations"]


def test_naive_or_missing_schedule_is_not_replaced_by_invented_hours() -> None:
    raw = _raw(3)
    schedule = _schedule(raw.index)
    schedule["market_open"] = schedule["market_open"].dt.tz_localize(None)
    bars, issues = _prepare(raw, schedule=schedule)
    assert bars.empty
    assert "invalid_schedule_timestamp" in set(issues["code"])
    schedule = _schedule(raw.index).iloc[:2]
    bars, issues = _prepare(raw, schedule=schedule)
    assert len(bars) == 2
    assert "session_missing_schedule" in set(issues["code"])


def test_preparation_does_not_mutate_raw_or_schedule() -> None:
    raw = _raw()
    schedule = _schedule(raw.index)
    original, original_schedule, attrs = raw.copy(deep=True), schedule.copy(deep=True), deepcopy(raw.attrs)
    _prepare(raw, schedule=schedule)
    pd.testing.assert_frame_equal(raw, original)
    pd.testing.assert_frame_equal(schedule, original_schedule)
    assert raw.attrs == attrs
