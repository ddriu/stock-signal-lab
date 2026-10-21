"""Fixed model cohorts, cash accounting and causal marks; no network or DB."""

from dataclasses import asdict, FrozenInstanceError

import pandas as pd
import pytest

from config import StrategyConfig
from src.model_results_engine import ExecutionCosts, FROZEN_MODELS, run_frozen_models
from src.strategy_audit_engine import AuditConfig, run_strategy_audit


def bars(prices, *, first="2025-01-02", currency="USD", **columns):
    days = pd.bdate_range(first, periods=len(prices))
    frame = pd.DataFrame(dict(open=prices, close=prices,
        high=[price * 1.01 for price in prices], low=[price * .99 for price in prices],
        fx_open=1.0, fx_close=1.0, reduce_setup=False, sell_setup=False, sma_long=1.0), index=days)
    frame["open_at"] = pd.to_datetime(days, utc=True) + pd.Timedelta(hours=14, minutes=30)
    frame["close_at"] = pd.to_datetime(days, utc=True) + pd.Timedelta(hours=21)
    for key, values in columns.items():
        frame[key] = values
    frame.attrs.update(display_currency=currency,
        session_schedule=[dict(session=day.date().isoformat(), open_at=row.open_at.isoformat(),
                               close_at=row.close_at.isoformat()) for day, row in frame.iterrows()],
        expected_sessions=[day.date().isoformat() for day in days],
        required_last_close_at=frame.iloc[-1].close_at.isoformat())
    return frame


def signal(frame, number=0, *, event=None, label="Entrada fuerte", score=85, ticker="AAA", **changes):
    row = dict(event_id=event or f"e{number}", ticker=ticker,
        signal_session=frame.index[number].date().isoformat(),
        observed_at=frame.iloc[number].close_at + pd.Timedelta(minutes=1),
        entry_label=label, entry_score=score)
    row.update(changes)
    return row


def run(frame, rows=None, **kwargs):
    return run_frozen_models(pd.DataFrame(rows if rows is not None else [signal(frame)]),
        {"AAA": frame}, start=frame.index[0].date().isoformat(),
        end=frame.index[-1].date().isoformat(), **kwargs)


def test_exact_four_frozen_models_and_strategy_configuration_unchanged():
    before = asdict(StrategyConfig())
    result = run(bars([100] * 5))
    assert list(result) == ["A", "B", "C", "D"]
    assert all(model.manifest["strategy_config"] == before for model in result.values())
    assert asdict(StrategyConfig()) == before
    with pytest.raises(FrozenInstanceError):
        FROZEN_MODELS["A"].amount_usd = 200
    with pytest.raises(TypeError):
        result["E"] = result["A"]


def test_daily_activation_threshold_and_identical_hold_cohort():
    frame = bars([100] * 9)
    rows = [signal(frame, 0, label="Esperar"), signal(frame, 1, score=84),
            signal(frame, 2, score=99), signal(frame, 3, label="Esperar"),
            signal(frame, 4, score=85), signal(frame, 5, label="Entrada interesante"),
            signal(frame, 6, score=84.999)]
    result = run(frame, rows)
    assert result["A"].lots.event_id.tolist() == ["e1", "e2", "e4", "e6"]
    assert result["B"].lots.event_id.tolist() == ["e1", "e4", "e6"]
    assert result["C"].lots.event_id.tolist() == ["e4"]
    assert result["D"].lots.event_id.tolist() == result["A"].lots.event_id.tolist()
    assert (result["A"].lots.contribution_usd == 100).all()
    assert result["D"].manifest["cohort_matches_A"] is True


def test_unknown_initial_activation_not_invented():
    frame = bars([100] * 4)
    result = run(frame)
    assert len(result["A"].lots) == 1
    assert result["B"].lots.empty and result["C"].lots.empty
    assert "left_censored_activation" in set(result["B"].issues.code)


def test_a_reduce_includes_sell_b_sell_only_and_d_never_exits():
    frame = bars([100] * 7, reduce_setup=[False, False, True, False, False, False, False],
        sell_setup=[False, False, False, False, True, False, False])
    rows = [signal(frame, label="Esperar"), signal(frame, 1)]
    result = run(frame, rows)
    assert result["A"].trades.iloc[0].exit_at == frame.iloc[3].open_at
    assert result["B"].trades.iloc[0].exit_at == frame.iloc[5].open_at
    assert result["D"].trades.empty
    only_sell = frame.copy(deep=True)
    only_sell["reduce_setup"] = False
    sold = run(only_sell, rows)["A"]
    assert sold.trades.iloc[0].exit_reason == "sell_setup"


def test_original_intraday_trailing_not_added_to_evaluator_models():
    frame = bars([100] * 5)
    frame.loc[frame.index[2], ["high", "low", "close"]] = [150, 90, 140]
    frame.loc[frame.index[3], ["open", "high", "low", "close"]] = [140, 141, 110, 120]
    result = run(frame)
    assert result["A"].trades.empty
    legacy = run_strategy_audit(pd.DataFrame([signal(frame)]), {"AAA": frame},
        AuditConfig(start=frame.index[0].date().isoformat(), end=frame.index[-1].date().isoformat(),
                    amount_usd=100, exit_policy="original"))
    assert not legacy.trades.empty


def test_close_stop_is_next_open_and_hold_can_recover():
    frame = bars([100, 100, 90, 80, 120])
    result = run(frame)
    trade = result["A"].trades.iloc[0]
    assert trade.decision_at == frame.iloc[2].close_at
    assert trade.exit_at == frame.iloc[3].open_at
    assert trade.exit_price_native == 80
    assert trade.exit_reason == "close_stop8"
    assert result["A"].metrics["realized_pnl_usd"] == pytest.approx(-20)
    assert result["D"].metrics["unrealized_pnl_usd"] == pytest.approx(20)


def test_theoretical_external_flows_are_unlimited_and_sale_cash_not_reused():
    frame = bars([100] * 7, reduce_setup=[False, True, False, False, False, False, False])
    result = run(frame, [signal(frame, 0), signal(frame, 2)])["A"]
    assert result.metrics["contributions_usd"] == 200
    assert result.metrics["cash_usd"] == 100
    assert result.metrics["final_value_usd"] == 200
    assert result.daily.external_flow_usd.sum() == 200
    assert result.daily.withdrawal_usd.sum() == 0
    assert result.metrics["fees_usd"] == 0


def test_budget_no_topups_full_entries_only_and_cash_recycling_explicit():
    frame = bars([100] * 7, reduce_setup=[False, True, False, False, False, False, False])
    result = run(frame, [signal(frame, 0), signal(frame, 2)], mode="budget", budget_usd=100,
                 costs=ExecutionCosts(0, 0, 0))
    a, d = result["A"], result["D"]
    assert len(a.lots) == 2
    assert (a.daily.cash_usd >= 0).all()
    assert a.daily.external_flow_usd.sum() == 100
    assert a.metrics["contributions_usd"] == 100
    assert a.metrics["invested_notional_usd"] == 200
    assert d.lots.event_id.tolist() == ["e0"]
    assert d.manifest["missing_A_event_ids"] == ["e2"]
    assert d.manifest["compatible_A_D_event_ids"] == ["e0"]
    assert d.metrics["cohort_matches_A"] is False
    assert "hold_cohort_incomplete" in set(d.issues.code)


def test_budget_reserves_exit_commission_and_cannot_overdraw_after_total_loss():
    frame = bars([100, 100, .0001, .0001, .0001])
    result = run(frame, mode="budget", budget_usd=102, costs=ExecutionCosts(1, 0, 0))["A"]
    assert len(result.lots) == 1
    assert result.metrics["cash_usd"] == pytest.approx(.0001)
    assert (result.daily.cash_usd >= -1e-9).all()
    insufficient = run(frame, mode="budget", budget_usd=101, costs=ExecutionCosts(1, 0, 0))["A"]
    assert insufficient.lots.empty
    assert insufficient.metrics["cash_usd"] == 101
    assert insufficient.manifest["budget_rejected_event_ids"] == ["e0"]


def test_budget_default_6000_and_estimated_costs_are_not_real_execution():
    result = run(bars([100] * 4), mode="budget")["A"]
    assert result.metrics["contributions_usd"] == 6000
    assert result.metrics["cash_usd"] == pytest.approx(5898.9)
    assert result.metrics["fees_usd"] == pytest.approx(1.1)
    assert result.manifest["is_real_execution"] is False
    assert result.manifest["costs"]["verified"] is False
    assert result.metrics["last_complete_contributions_usd"] == 6000
    assert result.metrics["last_complete_value_usd"] == pytest.approx(5998.9)
    assert result.metrics["last_complete_roi_pct"] == pytest.approx((5998.9 / 6000 - 1) * 100)


def test_fx_costs_only_non_usd_and_fixed_spread_costs_each_side():
    frame = bars([100] * 5, reduce_setup=[False, True, False, False, False])
    usd = run(frame, mode="budget", budget_usd=1000)["A"]
    foreign = frame.copy(deep=True)
    foreign.attrs["display_currency"] = "GBP"
    gbp = run(foreign, mode="budget", budget_usd=1000)["A"]
    assert usd.metrics["fees_usd"] == pytest.approx(2.2)
    assert gbp.metrics["fees_usd"] == pytest.approx(2.7)
    assert usd.metrics["realized_pnl_usd"] == pytest.approx(-2.2)
    assert gbp.metrics["final_value_usd"] == pytest.approx(997.3)
    assert usd.trades.iloc[0].pnl_usd == pytest.approx(-2.2)
    assert usd.lots.iloc[0].entry_price_native == 100


def test_simultaneous_known_exit_can_fund_entry_but_not_an_earlier_entry():
    frame = bars([100] * 6, reduce_setup=[False, True, False, False, False, False])
    result = run(frame, [signal(frame, 0), signal(frame, 1)], mode="budget", budget_usd=100,
                 costs=ExecutionCosts(0, 0, 0))["A"]
    assert len(result.lots) == 2
    assert result.lots.iloc[0].exit_at == result.lots.iloc[1].entry_at
    assert result.metrics["max_simultaneous_invested_capital_usd"] == 100


def test_missing_final_fx_keeps_total_unknown_and_does_not_bridge_twr():
    frame = bars([100] * 5, fx_close=[1, 1, None, 1, None])
    result = run(frame, mode="budget", budget_usd=1000, costs=ExecutionCosts(0, 0, 0))["A"]
    assert result.metrics["final_value_usd"] is None
    assert result.metrics["unrealized_pnl_usd"] is None
    assert result.metrics["cash_usd"] == 900
    assert result.metrics["twr_daily_approx_pct"] is None
    assert result.metrics["xirr_pct"] is None
    assert result.metrics["coverage_complete"] is False
    assert pd.isna(result.daily.iloc[3].unit_value_daily_approx)


def test_missing_intended_open_never_buys_a_later_convenient_quote():
    frame = bars([100, 100, 120, 130])
    absent = frame.drop(frame.index[1])
    result = run(absent)["A"]
    assert result.lots.empty
    assert "missing_intended_open" in set(result.issues.code)
    assert result.metrics["coverage_complete"] is False


def test_closed_market_can_carry_mark_only_from_authoritative_schedule():
    frame = bars([100, 100], first="2025-01-02")
    frame.attrs.update(calendar_coverage_start="2025-01-02", calendar_coverage_end="2025-01-05")
    result = run_frozen_models(pd.DataFrame([signal(frame)]), {"AAA": frame},
        start="2025-01-02", end="2025-01-05")["A"]
    assert result.metrics["final_value_usd"] == 100
    assert result.lots.iloc[0].valuation_carried_closed_market


def test_short_period_has_net_return_but_no_fake_annualized_xirr():
    result = run(bars([100, 100, 110]))["A"]
    assert result.metrics["net_return_pct"] == pytest.approx(10)
    assert result.metrics["xirr_pct"] is None
    assert "inferior a un año" in result.metrics["xirr_reason"]
    assert result.metrics["twr_daily_approx_pct"] == pytest.approx(10)
    assert result.metrics["twr_pct"] is None


def test_xirr_on_one_year_of_known_external_cashflows():
    frame = bars([100] * 270)
    frame.loc[frame.index[-1], ["open", "high", "low", "close"]] = [110, 111, 109, 110]
    result = run(frame)["D"]
    assert result.metrics["xirr_pct"] is not None
    assert 9 < result.metrics["xirr_pct"] < 11
    assert result.metrics["net_return_pct"] == pytest.approx(10)


def test_metrics_attribution_reconcile_and_trade_statistics_are_defined():
    frame = bars([100, 100, 110, 110, 100, 100],
        reduce_setup=[False, False, True, False, True, False])
    result = run(frame, [signal(frame, 0), signal(frame, 2)])["A"]
    assert len(result.trades) == 2
    assert result.metrics["win_rate_pct"] == 50
    assert result.metrics["profit_factor"] == pytest.approx(1.1)
    assert result.metrics["gain_loss_ratio"] == pytest.approx(1.1)
    assert result.metrics["turnover_pct"] > 0
    assert result.metrics["average_duration_days"] > 0
    assert sum(row["profit_usd"] for row in result.metrics["attribution_by_ticker"]) == pytest.approx(result.metrics["profit_usd"])
    assert result.metrics["max_simultaneous_invested_capital_usd"] == 100


def test_manifest_freezes_input_sources_costs_git_and_does_not_mutate_inputs():
    frame = bars([100] * 5)
    before = frame.copy(deep=True)
    signals = pd.DataFrame([signal(frame)])
    original_signals = signals.copy(deep=True)
    result = run_frozen_models(signals, {"AAA": frame}, start="2025-01-02", end="2025-01-08", git_sha="abc123")
    manifest = result["A"].manifest
    assert manifest["git_sha"] == "abc123"
    assert len(manifest["input_hash"]) == 64
    assert len(manifest["strategy_config_hash"]) == 64
    assert "src/signal_engine.py" in manifest["source_hashes"]
    assert len(manifest["source_hashes"]["src/signal_engine.py"]) == 64
    assert result["A"].manifest["model_run_hash"] != result["B"].manifest["model_run_hash"]
    pd.testing.assert_frame_equal(frame, before)
    pd.testing.assert_frame_equal(signals, original_signals)


def test_prices_and_signals_after_cutoff_cannot_change_results():
    frame = bars([100] * 8)
    cutoff = frame.index[4].date().isoformat()
    rows = pd.DataFrame([signal(frame, 0, label="Esperar"), signal(frame, 1)])
    before = run_frozen_models(rows, {"AAA": frame}, start="2025-01-02", end=cutoff)
    future = frame.copy(deep=True)
    future.loc[future.index[5]:, ["open", "high", "low", "close"]] = [1000, 1100, 900, 1050]
    appended = pd.concat([rows, pd.DataFrame([signal(frame, 5, event="future")])], ignore_index=True)
    after = run_frozen_models(appended, {"AAA": future}, start="2025-01-02", end=cutoff)
    for model_id in before:
        for field in ("daily", "lots", "trades", "issues"):
            pd.testing.assert_frame_equal(getattr(before[model_id], field), getattr(after[model_id], field))
        assert before[model_id].metrics == after[model_id].metrics


def test_unknown_exit_fx_cannot_recycle_a_hypothetical_sale_amount():
    frame = bars([100] * 7, reduce_setup=[False, True, False, False, False, False, False],
                 fx_open=[1, 1, None, 1, 1, 1, 1])
    result = run(frame, [signal(frame, 0), signal(frame, 2)], mode="budget", budget_usd=100,
                 costs=ExecutionCosts(0, 0, 0))["A"]
    assert result.lots.event_id.tolist() == ["e0"]
    assert result.metrics["cash_usd"] is None
    assert result.metrics["cash_known_usd"] == 0
    assert "unknown_sale_cash_not_recycled" in set(result.issues.code)
    assert result.manifest["budget_rejected_event_ids"] == ["e2"]


@pytest.mark.parametrize("kwargs", [dict(mode="actual"), dict(mode="budget", budget_usd=-1),
    dict(mode="budget", budget_usd=float("nan")), dict(budget_usd=100),
    dict(costs=ExecutionCosts(1, 0, 0)), dict(costs=ExecutionCosts(0, -1, 0)),
    dict(mode="budget", costs=ExecutionCosts(0, 10000, 0))])
def test_invalid_modes_budget_and_costs_rejected(kwargs):
    with pytest.raises(ValueError):
        run(bars([100] * 3), **kwargs)
