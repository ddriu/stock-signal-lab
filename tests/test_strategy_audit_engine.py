"""Small, synthetic, causality-focused strategy audits (no data downloads)."""

from dataclasses import replace

import pandas as pd
import pytest

from config import StrategyConfig
from src.strategy_audit_engine import AuditConfig, run_strategy_audit


def bars(prices, *, first="2026-01-02", fx=1.0, **columns):
    days = pd.bdate_range(first, periods=len(prices))
    frame = pd.DataFrame({"open": prices, "high": [price*1.01 for price in prices],
        "low": [price*.99 for price in prices], "close": prices,
        "fx_open": fx, "fx_close": fx, "reduce_setup": False,
        "sell_setup": False, "sma_long": 1.0}, index=days)
    frame["open_at"] = pd.to_datetime(days.strftime("%Y-%m-%d") + "T14:30:00Z")
    frame["close_at"] = pd.to_datetime(days.strftime("%Y-%m-%d") + "T21:00:00Z")
    for key, values in columns.items():
        frame[key] = values
    return frame


def signal(event="e1", day="2026-01-02", observed=None, label="Entrada fuerte", score=80, ticker="AAA", **changes):
    row = dict(event_id=event, ticker=ticker, signal_session=day,
               observed_at=observed or day + "T21:01:00Z", entry_label=label,
               entry_score=score, sector="Synthetic", company_score=80,
               valuation_score=60, opportunity_score=90, risk_score=50)
    row.update(changes)
    return row


def run(rows, frame, **changes):
    return run_strategy_audit(pd.DataFrame(rows), {"AAA": frame}, AuditConfig(**changes))


def test_buys_strictly_after_observation_at_next_open_and_never_same_close():
    frame = bars([100, 110, 120])
    result = run([signal()], frame, exit_policy="hold")
    lot = result.lots.iloc[0]
    assert lot.entry_at == frame.iloc[1].open_at
    assert lot.entry_price_native == 110
    assert lot.quantity == pytest.approx(1 / 110)
    assert result.daily.loc["2026-01-02", "contributions_usd"] == 0
    assert result.metrics["roi_pct"] == pytest.approx((120/110-1)*100)


def test_exact_open_observation_must_wait_another_open():
    frame = bars([100, 110, 120])
    result = run([signal(observed=frame.iloc[1].open_at)], frame, exit_policy="hold")
    assert result.lots.iloc[0].entry_at == frame.iloc[2].open_at


def test_delayed_close_cannot_sell_at_an_open_before_it_was_known():
    frame = bars([100, 100, 100, 100], sell_setup=[False, True, False, False])
    frame["close_available_at"] = frame.close_at
    frame.loc[frame.index[1], "close_available_at"] = frame.iloc[2].close_at
    result = run([signal()], frame, exit_policy="sell")
    assert result.trades.iloc[0].decision_at == frame.iloc[2].close_at
    assert result.trades.iloc[0].exit_at == frame.iloc[3].open_at
    early = run([signal()], frame, exit_policy="sell", end="2026-01-06T20:00:00Z")
    assert early.trades.empty
    assert pd.isna(early.daily.loc["2026-01-05", "value_usd"])


def test_position_stop_reads_frozen_config_not_a_hardcoded_eight():
    frame = bars([100, 100, 90, 95])
    wide = run([signal()], frame, exit_policy="sell", strategy_config=replace(StrategyConfig(), stop_loss_pct=15))
    assert wide.trades.empty
    default = run([signal()], frame, exit_policy="sell")
    assert default.trades.iloc[0].exit_reason == "close_stop8"


def test_recorded_intraday_signal_is_preserved_without_using_its_later_final_close():
    frame = bars([100, 110, 120])
    result = run([signal(observed="2026-01-02T18:00:00Z")], frame, exit_policy="hold")
    assert len(result.lots) == 1
    assert result.lots.iloc[0].entry_at == frame.iloc[1].open_at
    assert "intraday_observation_partial" in set(result.issues.code)
    assert result.metrics["coverage_complete"] is False


def test_first_session_observation_wins_not_later_strong_revision():
    result = run([signal(label="Esperar"), signal("later", observed="2026-01-02T22:00:00Z")], bars([100, 110]))
    assert result.lots.empty
    assert "later_session_observation_ignored" in set(result.issues.code)


def test_two_observed_strong_sessions_create_independent_lots_with_native_close_stops():
    frame = bars([100, 100, 110, 100, 105])
    result = run([signal(), signal("e2", "2026-01-05")], frame, exit_policy="sell")
    assert len(result.lots) == 2
    assert len(result.trades) == 1
    assert result.trades.iloc[0].event_id == "e2"
    assert result.trades.iloc[0].exit_reason == "close_stop8"
    assert result.trades.iloc[0].exit_price_native == 105
    assert result.lots.set_index("event_id").loc["e1", "status"] == "open"
    assert result.metrics["contributions_usd"] == 2


def test_reduce_and_sell_policies_differ_but_both_have_close_stop8():
    frame = bars([100, 100, 100, 100], reduce_setup=[True, True, False, False])
    reduced = run([signal()], frame, exit_policy="reduce")
    sold = run([signal()], frame, exit_policy="sell")
    assert len(reduced.trades) == 1
    assert reduced.trades.iloc[0].exit_at == frame.iloc[2].open_at
    assert sold.trades.empty


def test_exit_signal_before_entry_cannot_close_new_lot():
    frame = bars([100, 100, 100], reduce_setup=[True, False, False], sell_setup=[True, False, False])
    assert run([signal()], frame, exit_policy="reduce").trades.empty


def test_hold_recovers_without_following_stop_or_reduce_signals():
    frame = bars([100, 100, 70, 120], reduce_setup=True, sell_setup=True)
    result = run([signal()], frame, exit_policy="hold")
    assert result.trades.empty
    assert result.metrics["roi_pct"] == pytest.approx(20)
    assert result.metrics["unrealized_pnl_usd"] == pytest.approx(.2)


def test_gap_stop_fills_open_not_stop_level():
    frame = bars([100, 100, 80, 90])
    result = run([signal()], frame, exit_policy="fixed8")
    assert result.trades.iloc[0].exit_price_native == 80
    assert result.trades.iloc[0].fill_timing == "open_gap"
    assert result.metrics["roi_pct"] == pytest.approx(-20)


def test_trailing_does_not_apply_same_bar_high_before_low():
    frame = bars([100, 100, 110])
    frame.loc[frame.index[1], ["high", "low", "close"]] = [120, 95, 110]
    frame.loc[frame.index[2], ["high", "low", "close"]] = [111, 107, 110]
    result = run([signal()], frame, exit_policy="trailing10")
    assert len(result.trades) == 1
    assert result.trades.iloc[0].exit_at == frame.iloc[2].close_at
    assert result.trades.iloc[0].exit_price_native == pytest.approx(108)
    assert result.trades.iloc[0].fx_proxy


def test_stop_intraday_uses_open_fx_proxy_not_future_close_fx():
    frame = bars([100, 100, 100], fx_open=[1, 2, 2], fx_close=[1, 2, 9])
    frame.loc[frame.index[2], "low"] = 90
    result = run([signal()], frame, exit_policy="fixed8")
    assert result.trades.iloc[0].exit_fx == 2
    assert result.trades.iloc[0].proceeds_usd == pytest.approx(.92)
    assert "intraday_stop_fx_open_proxy" in set(result.issues.code)


def test_fx_movement_alone_never_triggers_native_stop():
    result = run([signal()], bars([100, 100, 100], fx_open=[1, 1, .5], fx_close=[1, 1, .5]), exit_policy="fixed8")
    assert result.trades.empty
    assert result.metrics["roi_pct"] == -50


def test_scheduled_original_exit_and_gap_stop_only_close_once():
    frame = bars([100, 100, 80], reduce_setup=[False, True, False])
    result = run([signal()], frame, exit_policy="original")
    assert len(result.trades) == 1
    assert result.trades.iloc[0].fill_timing == "next_open"
    assert result.trades.iloc[0].exit_price_native == 80


def test_original_exit_uses_existing_configuration_without_changing_it():
    frame = bars([100, 100, 89, 100])
    settings = StrategyConfig(stop_loss_pct=20, trailing_stop_pct=0, exit_on_reduce=False)
    result = run([signal()], frame, exit_policy="original", strategy_config=settings)
    assert result.trades.empty
    assert settings.stop_loss_pct == 20


def test_confirmation_counts_two_closes_after_entry_not_earlier_observations():
    frame = bars([90, 90, 90, 95], sma_long=100)
    result = run([signal()], frame, exit_policy="confirmed")
    assert result.trades.iloc[0].decision_at == frame.iloc[2].close_at
    assert result.trades.iloc[0].exit_at == frame.iloc[3].open_at


def test_daily_buys_do_not_fill_unobserved_days():
    frame = bars([100] * 12)
    result = run([signal(), signal("last", day=frame.index[10].date().isoformat())], frame, exit_policy="hold")
    assert len(result.lots) == 2
    assert result.metrics["contributions_usd"] == 2


def test_activation_skips_left_censored_and_repeated_strong_until_observed_transition():
    frame = bars([100] * 8)
    days = [day.date().isoformat() for day in frame.index]
    result = run([signal(day=days[0]), signal("repeat", days[2]),
                  signal("off", days[3], label="Esperar"), signal("on", days[5])], frame,
                 entry_mode="activation", exit_policy="hold")
    assert result.lots.event_id.tolist() == ["on"]
    assert "left_censored_activation" in set(result.issues.code)


@pytest.mark.parametrize("score,multiple", [(75, 1), (79, 1), (80, 2), (84, 2), (85, 3), (100, 3)])
def test_exact_predeclared_tier_sizing(score, multiple):
    result = run([signal(score=score)], bars([100, 100]), exit_policy="hold", sizing="tiered", amount_usd=2)
    assert result.lots.iloc[0].contribution_usd == multiple * 2


def test_factors_use_tier_times_worst_historical_score_and_do_not_substitute_current_data():
    result = run([signal(score=85)], bars([100, 100]), exit_policy="hold", sizing="factors", amount_usd=2)
    assert result.lots.iloc[0].contribution_usd == 3
    incomplete = run([signal(valuation_score=None)], bars([100, 100]), sizing="factors")
    assert incomplete.lots.empty
    assert incomplete.metrics["coverage_complete"] is False
    assert incomplete.metrics["unfilled_entries"] == 1


def test_missing_open_fx_does_not_fill_later_at_a_more_convenient_quote():
    result = run([signal()], bars([100, 100, 110], fx_open=[1, None, 1]), exit_policy="hold")
    assert result.lots.empty
    assert result.metrics["roi_pct"] is None
    assert result.metrics["coverage_complete"] is False


def test_missing_final_close_or_fx_keeps_total_unknown_and_known_subtotal_visible():
    result = run([signal()], bars([100, 100, 110], fx_close=[1, 1, None]), exit_policy="hold")
    assert result.metrics["final_value_usd"] is None
    assert result.metrics["roi_pct"] is None
    assert result.metrics["unrealized_pnl_usd"] is None
    assert result.metrics["value_known_usd"] == 0
    assert result.metrics["valuation_complete"] is False


def test_explicit_end_after_latest_cache_is_not_valued_at_an_older_close():
    frame = bars([100, 100, 110])
    result = run([signal()], frame, end="2026-01-07", exit_policy="hold")
    assert frame.index[-1].date().isoformat() == "2026-01-06"
    assert result.daily.index[-1] == "2026-01-07"
    assert result.metrics["final_value_usd"] is None
    assert result.metrics["valuation_complete"] is False


def test_invalid_next_open_timestamp_is_not_replaced_by_a_later_valid_open():
    frame = bars([100, 100, 120])
    frame.loc[frame.index[1], "open_at"] = pd.NaT
    result = run([signal()], frame, exit_policy="hold")
    assert result.lots.empty
    assert "unverifiable_next_open" in set(result.issues.code)


def test_missing_close_does_not_let_a_possible_exit_disappear_after_recovery():
    frame = bars([100, 100, 90, 120])
    frame.loc[frame.index[2], "close"] = None
    result = run([signal()], frame, exit_policy="sell")
    assert result.trades.empty
    assert result.lots.iloc[0].status == "unresolved"
    assert result.metrics["final_value_usd"] is None
    assert "missing_exit_close" in set(result.issues.code)


def test_missing_exit_fx_does_not_become_zero_proceeds_or_zero_loss():
    frame = bars([100, 100, 120], sell_setup=[False, True, False], fx_open=[1, 1, None])
    result = run([signal()], frame, exit_policy="sell")
    assert len(result.trades) == 1
    assert pd.isna(result.trades.iloc[0].pnl_usd)
    assert result.metrics["final_value_usd"] is None
    assert result.metrics["win_rate_pct"] is None


def test_sale_proceeds_are_not_reused_and_are_retained_in_total_wealth():
    frame = bars([100, 100, 120, 120, 120], reduce_setup=[False, True, False, False, False])
    result = run([signal(), signal("second", "2026-01-06")], frame, exit_policy="reduce")
    assert result.metrics["contributions_usd"] == 2
    assert result.metrics["withdrawals_usd"] == pytest.approx(1.2)
    assert result.metrics["final_value_usd"] == pytest.approx(2.2)
    assert result.metrics["fees_usd"] == result.metrics["tax_usd"] == 0
    assert result.metrics["twr_pct"] is None
    assert result.metrics["max_drawdown_pct"] is None
    assert result.metrics["twr_daily_approx_pct"] is not None


def test_duplicate_event_and_bar_do_not_duplicate_orders_but_conflicts_are_quarantined():
    frame = bars([100, 100, 100])
    duplicate_frame = pd.concat([frame, frame.iloc[[1]]])
    result = run([signal(), signal()], duplicate_frame, exit_policy="hold")
    assert len(result.lots) == 1
    assert len(set(result.lots.lot_id)) == 1
    conflict = run([signal(), signal(score=90)], frame, exit_policy="hold")
    assert conflict.lots.empty
    assert "conflicting_event_id" in set(conflict.issues.code)


def test_naive_availability_is_rejected_not_assumed_to_be_utc():
    frame = bars([100, 100])
    result = run([signal(observed="2026-01-02T21:01:00")], frame, exit_policy="hold")
    assert result.lots.empty
    assert "invalid_signal_availability" in set(result.issues.code)
    frame["open_at"] = frame.open_at.dt.tz_localize(None)
    result = run([signal()], frame)
    assert result.lots.empty
    assert "invalid_bar_availability" in set(result.issues.code)


def test_future_prices_and_future_signals_cannot_change_fixed_prefix():
    frame = bars([100, 100, 110, 105, 90, 200])
    config = AuditConfig(end="2026-01-06", exit_policy="hold")
    prefix = run_strategy_audit(pd.DataFrame([signal()]), {"AAA": frame.iloc[:3]}, config)
    appended = run_strategy_audit(pd.DataFrame([signal(), signal("future", "2026-01-08")]), {"AAA": frame}, config)
    pd.testing.assert_frame_equal(prefix.daily, appended.daily)
    pd.testing.assert_frame_equal(prefix.lots, appended.lots)
    assert prefix.metrics == appended.metrics


def test_future_quarantine_does_not_remove_pre_anomaly_history():
    frame = bars([100, 100, 110])
    frame.attrs.update(quarantined_from="2026-01-07", audit_blocked=True)
    result = run([signal()], frame, end="2026-01-06", exit_policy="hold")
    assert len(result.lots) == 1
    assert result.metrics["roi_pct"] == pytest.approx(10)
    extended = run([signal()], frame, end="2026-01-07", exit_policy="hold")
    assert len(extended.lots) == 1
    assert extended.metrics["roi_pct"] is None


def test_completely_blocked_assets_stay_in_missing_cohort_without_fake_returns():
    frame = bars([])
    frame.attrs["audit_blocked"] = True
    result = run([signal()], frame, end="2026-01-06", exit_policy="hold")
    assert result.metrics["coverage_complete"] is False
    assert result.manifest["eligible_tickers"] == ["AAA"]
    assert result.manifest["blocked_tickers"] == ["AAA"]
    assert result.manifest["unfilled_event_ids"] == ["e1"]


def schedule(frame):
    return [dict(session=index.date().isoformat(), open_at=row.open_at.isoformat(), close_at=row.close_at.isoformat())
            for index, row in frame.iterrows()]


def test_calendar_missing_intended_open_is_not_skipped_to_later_bar():
    complete = bars([100, 100, 120])
    missing = complete.drop(complete.index[1])
    missing.attrs["session_schedule"] = schedule(complete)
    result = run([signal()], missing, exit_policy="hold")
    assert result.lots.empty
    assert "missing_intended_open" in set(result.issues.code)
    assert result.metrics["coverage_complete"] is False


def test_missing_expected_session_does_not_hide_a_possible_exit_stop():
    complete = bars([100, 100, 100, 120])
    missing = complete.drop(complete.index[2])
    missing.attrs["session_schedule"] = schedule(complete)
    result = run([signal()], missing, exit_policy="fixed8")
    assert len(result.lots) == 1
    assert result.trades.empty
    assert result.lots.iloc[0].status == "unresolved"
    assert result.metrics["final_value_usd"] is None
    assert "unknown_intermediate_exit_path" in set(result.issues.code)


def test_authoritative_closed_market_holiday_can_carry_its_last_mark_only():
    american = bars([100, 100, 120])
    holiday = american.iloc[:2].copy()
    holiday.attrs["session_schedule"] = schedule(holiday)
    holiday.attrs.update(calendar_coverage_start=american.index[0].date().isoformat(),
                         calendar_coverage_end=american.index[-1].date().isoformat())
    observations = pd.DataFrame([signal(), signal("british", ticker="BBB")])
    result = run_strategy_audit(observations, {"AAA": american, "BBB": holiday}, AuditConfig(exit_policy="hold"))
    assert result.metrics["final_value_usd"] == pytest.approx(2.2)
    lot = result.lots.set_index("ticker").loc["BBB"]
    assert lot.valuation_session == "2026-01-05"
    assert lot.valuation_carried_closed_market
    holiday.attrs.clear()
    without_calendar = run_strategy_audit(observations, {"AAA": american, "BBB": holiday}, AuditConfig(exit_policy="hold"))
    assert without_calendar.metrics["final_value_usd"] is None


def test_expected_last_close_prevents_mistaking_truncated_schedule_for_a_holiday():
    full = bars([100, 100, 120])
    truncated = full.iloc[:2].copy()
    truncated.attrs.update(session_schedule=schedule(truncated),
        expected_sessions=[day.date().isoformat() for day in full.index],
        required_last_close_at=full.iloc[-1].close_at.isoformat())
    result = run([signal()], truncated, end="2026-01-06", exit_policy="hold")
    assert result.metrics["final_value_usd"] is None
    assert result.metrics["last_complete_valuation_date"] == "2026-01-05"
    assert result.metrics["last_complete_value_usd"] == 1


def test_inputs_are_not_mutated_and_manifest_and_ids_are_reproducible():
    frame, observations = bars([100, 100, 110]), pd.DataFrame([signal()])
    frame_before, signals_before = frame.copy(deep=True), observations.copy(deep=True)
    config = AuditConfig(exit_policy="hold")
    first = run_strategy_audit(observations, {"AAA": frame}, config)
    second = run_strategy_audit(observations, {"AAA": frame}, config)
    assert first.manifest == second.manifest
    pd.testing.assert_frame_equal(first.lots, second.lots)
    pd.testing.assert_frame_equal(frame, frame_before)
    pd.testing.assert_frame_equal(observations, signals_before)
    assert config == replace(config)


def test_truncated_calendar_cannot_call_all_later_dates_market_holidays():
    frame = bars([100, 100])
    frame.attrs.update(session_schedule=schedule(frame),
        required_last_close_at=frame.iloc[-1].close_at.isoformat())
    result = run([signal()], frame, end="2026-01-20", exit_policy="hold")
    assert result.metrics["final_value_usd"] is None
    assert result.metrics["coverage_complete"] is False
    assert result.metrics["last_complete_valuation_date"] == "2026-01-05"
    assert pd.isna(result.lots.iloc[0]["value_usd"])


def test_declared_calendar_can_prove_closed_weekend_but_not_an_expired_later_day():
    frame = bars([100, 100], first="2026-01-08")  # Thursday and Friday.
    frame.attrs.update(session_schedule=schedule(frame),
        calendar_coverage_start="2026-01-08", calendar_coverage_end="2026-01-11")
    observation = signal(day="2026-01-08")
    closed = run([observation], frame, end="2026-01-11", exit_policy="hold")
    assert closed.metrics["final_value_usd"] == 1
    assert closed.metrics["coverage_complete"] is True
    assert closed.lots.iloc[0]["valuation_carried_closed_market"]
    expired = run([observation], frame, end="2026-01-12", exit_policy="hold")
    assert expired.metrics["final_value_usd"] is None
    assert expired.metrics["coverage_complete"] is False
    assert not expired.lots.iloc[0]["valuation_carried_closed_market"]


def test_calendar_range_does_not_turn_a_missing_expected_final_bar_into_a_closure():
    full = bars([100, 100, 120])
    frame = full.iloc[:2].copy()
    frame.attrs.update(session_schedule=schedule(full),
        calendar_coverage_start="2026-01-02", calendar_coverage_end="2026-01-06")
    result = run([signal()], frame, end="2026-01-06", exit_policy="hold")
    assert result.metrics["final_value_usd"] is None
    assert result.metrics["coverage_complete"] is False
    assert "missing_expected_bar" in set(result.issues["code"])
