"""Offline same-budget comparison: no retrospective signal reconstruction."""
from copy import deepcopy
from datetime import date

import pandas as pd
import pytest

from src.model_execution_comparison import build_execution_comparison, fx_records_from_prepared_bars
from src.model_results_engine import ExecutionCosts, run_frozen_models
from src.strategy_audit_data import AuditSignalSet


BASIS = "Prior available daily close; conservative availability next day 02:00 UTC"


def inputs(currency="EUR"):
    days = pd.bdate_range(end="2026-10-30", periods=40)
    raw = pd.DataFrame({"open": 99.5, "high": 101., "low": 99., "close": 100., "volume": 100_000.}, index=days)
    raw.iloc[-5] = [100., 101.2, 99.8, 101., 200_000.]
    raw.iloc[-4] = [101., 101.5, 100., 100.5, 100_000.]
    raw.iloc[-3] = [101., 102.2, 100.8, 102., 200_000.]
    raw.iloc[-2] = [102., 103.2, 101.8, 103., 200_000.]
    prepared = raw.copy(deep=True)
    prepared["open_at"] = pd.to_datetime(days, utc=True) + pd.Timedelta(hours=14, minutes=30)
    prepared["close_at"] = pd.to_datetime(days, utc=True) + pd.Timedelta(hours=21)
    prepared["fx_open"] = 1.2 if currency == "EUR" else .0075 if currency == "JPY" else 1.
    prepared["fx_close"] = prepared.fx_open
    prepared["reduce_setup"] = False
    prepared["sell_setup"] = False
    prepared["sma_long"] = 1.
    prepared.attrs.update(display_currency=currency,
        session_schedule=[{"session": day.date().isoformat(), "open_at": row.open_at.isoformat(),
            "close_at": row.close_at.isoformat()} for day, row in prepared.iterrows()],
        expected_sessions=[day.date().isoformat() for day in days],
        required_last_close_at=prepared.iloc[-1].close_at.isoformat())
    records = [{"event_id": f"e{number}", "ticker": "TEST", "signal_session": days[number].date().isoformat(),
        "observed_at": prepared.iloc[number].close_at + pd.Timedelta(minutes=1),
        "entry_label": "Esperar" if number in {34, 36} else "Entrada fuerte", "entry_score": 86,
        "currency": currency} for number in range(34, 39)]
    observed = AuditSignalSet(pd.DataFrame(records), pd.DataFrame(), {"signals": 5}, {"source": "growth", "cohort": "raw"})
    fx_days = days[-10:]
    records_fx = {key: pd.DataFrame({"economic_day": [day.date() for day in fx_days], "close": value,
        "available_at": pd.to_datetime(fx_days, utc=True) + pd.Timedelta(hours=15, minutes=30)})
        for key, value in (("EUR", 1.2), ("JPY", .0075))}
    return observed, {"TEST": prepared}, {"TEST": raw}, {"TEST": {"currency": currency}}, records_fx


def build(currency="EUR", **changes):
    observed, bars, raw, metadata, fx = inputs(currency)
    kwargs = {"raw_frames": raw, "metadata": metadata, "fx_records": fx,
        "start": "2026-10-23", "end": "2026-10-30", "git_sha": "test-fixed-version"}
    kwargs.update(changes)
    return build_execution_comparison(observed, bars, **kwargs)


def test_three_variants_twelve_frozen_models_same_budget_and_costs():
    report = build()
    assert set(report.models) == {"baseline", "liquidity_eur", "execution_setup"}
    assert len(report.summary) == 12
    for group in report.models.values():
        assert list(group) == ["A", "B", "C", "D"]
        for result in group.values():
            assert result.manifest["budget_usd"] == 6000
            assert result.manifest["costs"] == {"commission_fixed_usd": 1., "spread_bps": 10., "fx_bps": 25., "verified": False}
            assert result.manifest["is_real_execution"] is False
    assert not report.decisions.empty and not report.daily.empty and not report.lots.empty


def test_baseline_exactly_matches_unchanged_model_engine_and_all_inputs_remain_unchanged():
    observed, bars, raw, metadata, fx = inputs()
    before = deepcopy((observed, bars, raw, metadata, fx))
    report = build_execution_comparison(observed, bars, raw_frames=raw, metadata=metadata, fx_records=fx,
        start="2026-10-23", end="2026-10-30", budget_usd=6000, git_sha="same")
    direct = run_frozen_models(observed.signals, bars, start="2026-10-23", end="2026-10-30",
        mode="budget", budget_usd=6000, costs=ExecutionCosts(), git_sha="same")
    for model in direct:
        pd.testing.assert_frame_equal(report.models["baseline"][model].lots, direct[model].lots)
        assert report.models["baseline"][model].manifest["model_run_hash"] == direct[model].manifest["model_run_hash"]
    pd.testing.assert_frame_equal(observed.signals, before[0].signals)
    pd.testing.assert_frame_equal(bars["TEST"], before[1]["TEST"])
    pd.testing.assert_frame_equal(raw["TEST"], before[2]["TEST"])
    assert metadata == before[3]
    pd.testing.assert_frame_equal(fx["EUR"], before[4]["EUR"])


def test_nonentry_observations_are_retained_so_activation_state_is_not_erased():
    report = build()
    expected_ids = report.observations.signals.event_id.tolist()
    for variant, signals in report.signals.items():
        assert signals.event_id.tolist() == expected_ids
        assert len(report.models[variant]["B"].lots) == 2
    assert "effective filtered strong state" in report.manifest["activation_rule"]


def test_yen_nominal_turnover_is_not_mistaken_for_eur_liquidity_or_changed_score():
    report = build("JPY")
    assert len(report.models["baseline"]["A"].lots) == 3
    assert report.models["liquidity_eur"]["A"].lots.empty
    assert report.models["execution_setup"]["A"].lots.empty
    rows = report.decisions.loc[report.decisions.eligible_baseline]
    assert rows.adv_eur.between(60_000, 90_000).all()
    assert report.signals["liquidity_eur"].entry_score.eq(86).all()
    assert report.signals["liquidity_eur"].execution_raw_entry_label.eq("Entrada fuerte").sum() == 3


def test_future_fx_quotes_do_not_change_historical_context_or_filtered_fills():
    observed, bars, raw, metadata, fx = inputs("JPY")
    first = build_execution_comparison(observed, bars, raw_frames=raw, metadata=metadata, fx_records=fx,
        start="2026-10-23", end="2026-10-30")
    future = {key: pd.concat([frame, pd.DataFrame({"economic_day": [date(2026, 11, 2)], "close": [999.],
        "available_at": [pd.Timestamp("2026-11-02T15:30:00Z")]})], ignore_index=True) for key, frame in fx.items()}
    later = build_execution_comparison(observed, bars, raw_frames=raw, metadata=metadata, fx_records=future,
        start="2026-10-23", end="2026-10-30")
    pd.testing.assert_frame_equal(first.decisions, later.decisions)
    pd.testing.assert_frame_equal(first.models["execution_setup"]["A"].lots, later.models["execution_setup"]["A"].lots)


def test_fx_pairs_must_share_an_economic_day_and_be_known_at_signal_time():
    observed, bars, raw, metadata, fx = inputs("JPY")
    fx["EUR"]["economic_day"] = date(2026, 1, 2)
    report = build_execution_comparison(observed, bars, raw_frames=raw, metadata=metadata, fx_records=fx,
        start="2026-10-23", end="2026-10-30")
    assert report.decisions.loc[report.decisions.eligible_baseline, "adv_eur"].isna().all()
    assert report.models["liquidity_eur"]["A"].lots.empty


def test_prepared_fx_helper_infers_only_the_declared_exact_protocol_not_stock_session():
    _, bars, _, _, _ = inputs("EUR")
    frame = bars["TEST"]
    frame["fx_close_available_at"] = pd.Timestamp("2026-10-29T02:00:00Z")
    assert fx_records_from_prepared_bars(bars) == {}
    result = fx_records_from_prepared_bars(bars, fx_basis=BASIS)
    assert result["EUR"].economic_day.tolist() == [date(2026, 10, 28)]
    assert result["EUR"].available_at.tolist() == [pd.Timestamp("2026-10-29T02:00:00Z")]
    frame["fx_close_available_at"] = pd.Timestamp("2026-10-29T03:00:00Z")
    assert fx_records_from_prepared_bars(bars, fx_basis=BASIS) == {}


def test_prepared_fx_helper_quarantines_conflicting_duplicate_rates():
    _, bars, _, _, _ = inputs("EUR")
    bars["TEST"]["fx_close_available_at"] = pd.Timestamp("2026-10-29T02:00:00Z")
    other = bars["TEST"].copy()
    other["fx_close"] = 2.
    assert fx_records_from_prepared_bars({**bars, "OTHER": other}, fx_basis=BASIS) == {}


def test_prepared_fx_helper_prefers_explicit_economic_provenance_without_assuming_protocol():
    _, bars, _, _, _ = inputs("EUR")
    bars["TEST"]["fx_close_available_at"] = pd.Timestamp("2026-10-29T15:30:00Z")
    bars["TEST"]["fx_close_economic_day"] = date(2026, 10, 29)
    result = fx_records_from_prepared_bars(bars)
    assert result["EUR"].economic_day.tolist() == [date(2026, 10, 29)]


def test_explicit_benchmarks_use_same_single_budget_not_trade_reinvestment_flows():
    _, bars, _, _, _ = inputs()
    references = {ticker: bars["TEST"].copy() for ticker in ("SPY", "QQQ", "VT")}
    for frame in references.values(): frame.attrs["display_currency"] = "USD"
    report = build(benchmark_bars=references, include_matched_hold=True)
    assert len(report.benchmarks) == 36
    assert report.benchmarks.external_contributions_usd.eq(6000).all()
    assert set(report.matched_hold) == {"baseline", "liquidity_eur", "execution_setup"}
