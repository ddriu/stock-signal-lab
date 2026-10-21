"""Cross-module audit regressions using synthetic data only.

The legacy backtest's capital allocator is deliberately NOT equated with the
audit's externally funded independent lots. A single fully funded USD entry,
with no costs or FX, makes their configured exit mechanics comparable.
"""

from dataclasses import replace

import pandas as pd
import pytest

from config import BacktestConfig, StrategyConfig
from src.backtesting import run_backtest
from src.strategy_audit_diagnostics import exit_counterfactuals
from src.strategy_audit_engine import AuditConfig, run_strategy_audit
from src.strategy_audit_service import matched_benchmark, open_scenarios


def _bars(path="trailing"):
    days = pd.bdate_range("2026-10-05", periods=6)
    frame = pd.DataFrame(
        dict(open=100.0, high=101.0, low=99.0, close=100.0,
             fx_open=1.0, fx_close=1.0, sma_long=80.0,
             signal_score=85, buy_setup=False, reduce_setup=False, sell_setup=False),
        index=days,
    )
    frame.loc[days[0], "buy_setup"] = True
    if path == "trailing":
        frame.loc[days[1], ["high", "low", "close"]] = [120, 95, 110]
        frame.loc[days[2], ["open", "high", "low", "close"]] = [110, 111, 107, 110]
        frame.loc[days[3]:, ["open", "high", "low", "close"]] = [110, 111, 109, 110]
    elif path in {"hard_gap", "signal_gap"}:
        frame.loc[days[2], ["open", "high", "low", "close"]] = [80, 81, 79, 80]
        if path == "signal_gap":
            frame.loc[days[1], "reduce_setup"] = True
    elif path == "intraday_recovery":
        frame.loc[days[1], ["high", "low", "close"]] = [110, 90, 105]
    else:
        raise ValueError(path)
    frame["open_at"] = pd.to_datetime(days, utc=True) + pd.Timedelta(hours=13, minutes=30)
    frame["close_at"] = pd.to_datetime(days, utc=True) + pd.Timedelta(hours=20)
    frame.attrs.update(
        session_schedule=[dict(session=day.date().isoformat(), open_at=row.open_at.isoformat(),
                               close_at=row.close_at.isoformat()) for day, row in frame.iterrows()],
        expected_sessions=[day.date().isoformat() for day in days],
        required_last_close_at=frame.iloc[-1].close_at.isoformat(),
    )
    return frame


def _signals(frame, *, ticker="TEST", event_id="entry"):
    return pd.DataFrame([dict(event_id=event_id, ticker=ticker,
        observed_at=frame.iloc[0].close_at + pd.Timedelta(minutes=1),
        signal_session=frame.index[0].date().isoformat(), entry_label="Entrada fuerte",
        entry_score=85, sector="Synthetic", company_score=80, valuation_score=70,
        opportunity_score=90, risk_score=75)])


def _config(frame, **kwargs):
    # Exactly one 100% allocation is the controlled comparison, not a claim
    # that external per-signal contributions reproduce the legacy cash model.
    strategy = StrategyConfig(sma_short=1, sma_medium=2, sma_long=3,
                              max_risk_per_trade_pct=8, stop_loss_pct=8)
    return AuditConfig(end=frame.index[-1].date().isoformat(), amount_usd=100,
                       exit_policy="original", strategy_config=strategy, **kwargs)


@pytest.mark.parametrize("path,legacy_reason,fill_timing", [
    ("trailing", "Trailing stop", "intraday_unknown_before_close"),
    ("hard_gap", "Stop loss", "open_gap"),
    ("signal_gap", "Señal", "next_open"),
])
def test_original_exit_fills_match_legacy_when_cash_models_are_comparable(path, legacy_reason, fill_timing):
    frame = _bars(path)
    config = _config(frame)
    legacy = run_backtest(frame, config.strategy_config,
                         BacktestConfig(initial_capital=100, commission_pct=0, slippage_pct=0))
    audit = run_strategy_audit(_signals(frame), {"TEST": frame}, config)

    assert len(legacy.trades) == len(audit.trades) == 1
    old, current = legacy.trades.iloc[0], audit.trades.iloc[0]
    assert current.entry_at.date() == old.entry_date.date()
    assert current.exit_at.date() == old.exit_date.date()
    assert current.entry_price_native == pytest.approx(old.entry_price)
    assert current.exit_price_native == pytest.approx(old.exit_price)
    assert current.quantity == pytest.approx(old.quantity)
    assert current.pnl_usd == pytest.approx(old.pnl)
    assert current.return_pct == pytest.approx(old.return_pct)
    assert old.exit_reason == legacy_reason
    assert current.fill_timing == fill_timing
    assert current.fees_usd == current.tax_usd == 0
    # Before the first contribution the audit has zero wealth, unlike funded
    # legacy cash. Once funded, both paths must retain identical sale proceeds.
    pd.testing.assert_series_equal(
        audit.daily.value_usd.iloc[1:].reset_index(drop=True),
        legacy.equity_curve.equity.iloc[1:].reset_index(drop=True), check_names=False,
    )
    assert audit.metrics["final_value_usd"] == pytest.approx(legacy.metrics["final_equity"])


def test_current_real_position_labels_and_costs_cannot_change_virtual_results():
    frame = _bars()
    signals = _signals(frame)
    config = _config(frame)
    baseline = run_strategy_audit(signals, {"TEST": frame}, config)
    unrelated_real_position = signals.assign(position_label="Vender", position_score=0,
        position_entry_price=10_000_000, position_quantity=-999,
        position_cost_basis_eur=1_000_000, position_gain_loss_pct=-99)
    other_label = unrelated_real_position.assign(position_label="Ampliar", position_entry_price=0.01)

    for observations in (unrelated_real_position, other_label):
        report = run_strategy_audit(observations, {"TEST": frame}, config)
        for name in ("lots", "trades", "daily", "issues"):
            pd.testing.assert_frame_equal(getattr(report, name), getattr(baseline, name))
        assert report.metrics == baseline.metrics


def test_fixed_cutoff_original_and_diagnostics_ignore_future_prices_and_signals():
    full = _bars()
    prefix = full.iloc[:4].copy()
    prefix.attrs.update(session_schedule=full.attrs["session_schedule"][:4],
                        expected_sessions=full.attrs["expected_sessions"][:4],
                        required_last_close_at=prefix.iloc[-1].close_at.isoformat())
    config = _config(prefix)
    observations = _signals(full)
    future = observations.iloc[0].to_dict()
    future.update(event_id="future", observed_at=full.iloc[4].close_at + pd.Timedelta(minutes=1),
                  signal_session=full.index[4].date().isoformat())
    appended = pd.concat([observations, pd.DataFrame([future])], ignore_index=True)
    full.loc[full.index[4]:, ["open", "high", "low", "close"]] = [1000, 1100, 900, 1050]
    full.attrs["quarantined_from"] = full.index[5].date().isoformat()

    before = run_strategy_audit(observations, {"TEST": prefix}, config)
    after = run_strategy_audit(appended, {"TEST": full}, config)
    before_hold = run_strategy_audit(observations, {"TEST": prefix}, replace(config, exit_policy="hold"))
    after_hold = run_strategy_audit(appended, {"TEST": full}, replace(config, exit_policy="hold"))
    for name in ("lots", "trades", "daily", "issues"):
        pd.testing.assert_frame_equal(getattr(before, name), getattr(after, name))
    assert before.metrics == after.metrics
    pd.testing.assert_frame_equal(
        exit_counterfactuals(before, before_hold, {"TEST": prefix}, end=config.end),
        exit_counterfactuals(after, after_hold, {"TEST": full}, end=config.end),
    )
    assert matched_benchmark(before.lots, prefix, config.end) == matched_benchmark(after.lots, full, config.end)


def test_intraday_stop_counterfactual_can_recover_by_that_same_session_close():
    frame = _bars("intraday_recovery")
    config = replace(_config(frame), end=frame.index[1].date().isoformat(), exit_policy="fixed8")
    observed = _signals(frame)
    report = run_strategy_audit(observed, {"TEST": frame}, config)
    hold = run_strategy_audit(observed, {"TEST": frame}, replace(config, exit_policy="hold"))
    row = exit_counterfactuals(report, hold, {"TEST": frame}, end=config.end).iloc[0]

    assert report.trades.iloc[0].fill_timing == "intraday_unknown_before_close"
    assert report.trades.iloc[0].proceeds_usd == pytest.approx(92)
    assert row.same_lot_hold_value_usd == pytest.approx(105)
    assert row.missed_terminal_gain_usd == pytest.approx(13)
    assert row.first_post_exit_breakeven_at == frame.iloc[1].close_at
    assert row.sessions_to_breakeven == 1
    assert row.recovery_path_complete
    assert row.recovery_timing == "same-session close; intraday stop time unknown"
    assert row.comparison_complete


def test_engine_unresolved_lot_never_becomes_zero_in_scenarios_or_benchmark_coverage():
    complete = _bars("hard_gap")
    # The missing expected session might have stopped BBB; do not silently
    # turn that unknown path into a profitable open holding or zero exposure.
    missing = complete.drop(complete.index[2])
    observed = pd.concat([_signals(complete, ticker="AAA", event_id="a"),
                          _signals(complete, ticker="BBB", event_id="b")], ignore_index=True)
    report = run_strategy_audit(observed, {"AAA": complete, "BBB": missing}, _config(complete))
    assert report.lots.set_index("ticker").status.to_dict() == {"AAA": "closed", "BBB": "unresolved"}
    assert report.metrics["withdrawals_usd"] == pytest.approx(80)
    assert report.metrics["final_value_usd"] is None
    assert report.metrics["coverage_complete"] is False
    scenarios = open_scenarios(report.lots, report.metrics["withdrawals_usd"])
    assert scenarios.wealth_usd.isna().all()
    assert scenarios.open_value_usd.isna().all()

    reference = matched_benchmark(report.lots, complete, _config(complete).end)
    assert reference["contributions_usd"] == pytest.approx(200)
    assert reference["value_usd"] == pytest.approx(200)
    # A fully observed benchmark must not repair the strategy's missing path.
    assert reference["data_complete"] is True
    assert report.metrics["coverage_complete"] is False
    missing_entry = complete.drop(complete.index[1])
    pending_reference = matched_benchmark(report.lots, missing_entry, _config(complete).end)
    assert pending_reference["pending_contributions"] == 2
    assert pending_reference["value_usd"] is None
