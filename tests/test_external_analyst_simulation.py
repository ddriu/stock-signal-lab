"""Deterministic quotes and calls; no network, private mail or broker access."""
from copy import deepcopy

import pytest

from src.external_analyst_simulation import (
    SimulationConfig, SimulationCosts, simulate_external_calls, simulate_four_strategies,
)


ZERO = SimulationCosts(commission_eur=0, spread_bps=0, slippage_bps=0, fx_fee_bps=0)
AS_OF = "2026-11-13T18:00:00Z"


def test_missing_middle_session_cannot_hide_stop_before_later_target():
    missing = bar("11", data_issue="provider_missing_session")
    result = simulate(bars=[bar(), missing, bar("12", high=125.)])
    assert not result["trades"]
    assert result["statuses"]["one"] == "not_verifiable"
    assert result["open_positions"][0]["mark_is_stale"]
    assert result["metrics"]["hit_rate_pct"] is None
    assert not result["metrics"]["reliable"]
    no_marker = simulate(bars=[bar(), bar("12", high=125.)])
    assert no_marker["simulation_id"] != result["simulation_id"]


def test_split_is_not_a_stop_loss_or_win_without_position_adjustment():
    result = simulate(bars=[bar(), bar("11", stock_splits=2., open=50., high=52., low=49., close=51.)])
    assert not result["trades"] and result["statuses"]["one"] == "not_verifiable"


def test_irrelevant_broken_prices_do_not_change_result():
    original = simulate()
    assert simulate(bars=[bar(), {"ticker": "IRRELEVANT"}]) == original


def test_effective_entry_cost_cannot_exceed_target():
    result = simulate([call(target=100.5)], [bar(high=100.4, close=100.2)],
                      costs=SimulationCosts(commission_eur=1, spread_bps=100, slippage_bps=100))
    assert not result["events"] and not result["open_positions"]


@pytest.mark.parametrize("price", [90., 120., 130., 0.])
def test_price_already_outside_target_or_stop_when_observed_is_not_later_reused(price):
    result = simulate([call(reference_price=price)])
    assert not result["trades"] and not result["events"]
    assert result["statuses"]["one"] == "not_verifiable"


def call(identity="one", ticker="AAA", **changes):
    return {
        "id": identity, "provider": "perito_inversor", "source_id": identity,
        "company_name": "Sociedad sintética", "ticker": ticker, "currency": "EUR",
        "analyst": "Analista sintético", "published_at": "2026-11-09T12:00:00Z",
        "received_at": "2026-11-10T09:00:00Z", "observed_at": "2026-11-10T09:01:00Z",
        "action": "buy", "direction": "long", "entry": 100., "stop": 90., "target": 120.,
        "reference_price": 100., "source_url": "https://www.peritoinversor.com/#/radar",
        "source_status": "pending", "entry_rule": "limit", "executable": True,
        **changes,
    }


def bar(day="10", ticker="AAA", **changes):
    return {
        "ticker": ticker, "currency": "EUR",
        "open_at": f"2026-11-{day}T10:00:00Z", "close_at": f"2026-11-{day}T16:00:00Z",
        "available_at": f"2026-11-{day}T16:01:00Z",
        "open": 100., "high": 105., "low": 95., "close": 102.,
        **changes,
    }


def simulate(calls=None, bars=None, **kwargs):
    return simulate_external_calls(calls or [call()], bars or [bar()],
                                   as_of=kwargs.pop("as_of", AS_OF), costs=kwargs.pop("costs", ZERO), **kwargs)


def test_no_execution_in_signal_session_if_its_open_was_already_known():
    recommendation = call(received_at="2026-11-10T10:00:00Z", observed_at="2026-11-10T10:00:00Z")
    result = simulate([recommendation], [bar(), bar("11")])
    entries = [event for event in result["events"] if event["event_type"] == "entry_activated"]
    assert len(entries) == 1
    assert entries[0]["executed_at"] == "2026-11-11T10:00:00+00:00"
    assert any(row["code"] == "opening_not_after_observation" for row in result["incidents"])


def test_late_import_uses_observation_not_historical_mail_receipt():
    recommendation = call(observed_at="2026-11-11T11:00:00Z")
    result = simulate([recommendation], [bar(), bar("11"), bar("12")])
    entries = [event for event in result["events"] if event["event_type"] == "entry_activated"]
    assert entries[0]["executed_at"].startswith("2026-11-12T10:00")
    assert entries[0]["known_at"].startswith("2026-11-11T11:00")


def test_prefix_is_unchanged_by_future_prices_calls_or_source_revisions():
    cutoff = "2026-11-10T17:00:00Z"
    original = simulate([call()], [bar()], as_of=cutoff)
    future_call = call("future", ticker="BBB", published_at="2026-11-12T12:00:00Z",
                       received_at="2026-11-12T12:00:00Z", observed_at="2026-11-12T12:01:00Z")
    future_revision = call(target=800., observed_at="2026-11-13T09:01:00Z")
    extended = simulate([future_revision, future_call, call()], [bar("13", high=900.), bar()], as_of=cutoff)
    assert extended == original


def test_open_candle_or_unpublished_ohlc_cannot_be_used():
    result = simulate(as_of="2026-11-10T15:00:00Z")
    assert result["cash_eur"] == 100 and result["events"] == []
    assert result["statuses"]["one"] == "not_verifiable"


@pytest.mark.parametrize("status", ["target_reached", "stopped", "expired", "cancelled", "historical"])
def test_source_already_terminal_is_not_a_historical_win(status):
    result = simulate([call(source_status=status)], [bar(high=130.)])
    assert not result["events"] and not result["trades"]
    assert result["statuses"]["one"] == "historical_not_executable"
    assert result["metrics"]["net_return_pct"] == 0
    assert result["metrics"]["hit_rate_pct"] is None
    assert result["by_analyst"]["Analista sintético"]["closed_trades"] == 0


def test_later_revision_cannot_delete_or_reprice_an_already_frozen_trade():
    original = call()
    later = call(target=200., stop=None, source_status="target_reached",
                 observed_at="2026-11-12T09:01:00Z")
    bars = [bar(), bar("11", high=125.)]
    before = simulate([original], bars)
    after = simulate([later, original], bars)
    assert after["trades"] == before["trades"]
    assert after["metrics"] == before["metrics"]
    assert after["cash_eur"] == before["cash_eur"] == 120.
    assert any(row["code"] == "later_revision_not_applied_to_frozen_trade" for row in after["incidents"])


def test_repeat_delivery_at_another_time_is_not_a_conflicting_forecast():
    original = call()
    receipt = call(received_at="2026-11-11T09:00:00Z", observed_at="2026-11-11T09:01:00Z")
    bars = [bar(), bar("11", high=125.)]
    before = simulate([original], bars)
    after = simulate([receipt, original], bars)
    assert after["trades"] == before["trades"]
    assert after["metrics"] == before["metrics"]
    assert not any("revision" in row["code"] for row in after["incidents"])


def test_malformed_future_bar_cannot_change_a_verified_prefix():
    cutoff = "2026-11-10T17:00:00Z"
    before = simulate([call()], [bar()], as_of=cutoff)
    malformed_future = {"ticker": "AAA", "available_at": "2026-11-12T16:01:00Z", "low": "broken"}
    after = simulate([call()], [malformed_future, bar()], as_of=cutoff)
    assert after == before


def test_pence_quotes_are_not_treated_as_full_pounds_in_the_simulator():
    result = simulate([call(currency="GBP")], [bar(currency="GBp", fx_eur_per_unit=1.1,
                      fx_observed_at="2026-11-10T08:00:00Z")])
    assert not result["events"] and result["cash_eur"] == 100
    assert result["statuses"]["one"] == "not_verifiable"


@pytest.mark.parametrize("changes", [
    {"entry_rule": "unknown"}, {"stop": None}, {"currency": None},
    {"entry": 0}, {"stop": 120}, {"target": 80}, {"executable": False},
    {"action": "sell", "direction": "short"}, {"received_at": "2026-11-10T09:00:00"},
])
def test_missing_fields_unknown_rules_and_short_sales_are_not_invented(changes):
    result = simulate([call(**changes)])
    assert result["cash_eur"] == result["nav_eur"] == 100
    assert not result["events"] and result["statuses"]["one"] == "not_verifiable"
    assert result["metrics"]["hit_rate_pct"] is None


def test_limit_waits_and_breakout_needs_the_explicit_trigger():
    limit = simulate([call()], [bar(open=110., high=115., low=105., close=108.), bar("11", open=98.)])
    entry = next(event for event in limit["events"] if event["event_type"] == "entry_activated")
    assert entry["executed_at"].startswith("2026-11-11T10:00") and entry["price_native"] == 98.
    breakout = simulate([call(entry_rule="breakout")],
                        [bar(open=95., high=99., low=93., close=98.), bar("11", open=101.)])
    entry = next(event for event in breakout["events"] if event["event_type"] == "entry_activated")
    assert entry["executed_at"].startswith("2026-11-11T10:00") and entry["price_native"] == 101.


def test_explicit_market_order_uses_later_open_not_source_price():
    result = simulate([call(entry_rule="market")], [bar(open=107., high=109., low=102., close=108.)])
    entry = next(event for event in result["events"] if event["event_type"] == "entry_activated")
    assert entry["price_native"] == 107.
    assert result["open_positions"][0]["quantity"] == pytest.approx(100 / 107)


def test_both_stop_and_target_is_a_conservative_ambiguous_loss_not_a_win():
    result = simulate(bars=[bar(high=125., low=85.)])
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["status"] == "ambiguous_conservative_stop"
    assert trade["exit_price_native"] == 90 and trade["pnl_eur"] == pytest.approx(-10)
    assert not trade["verified"] and result["cash_eur"] == pytest.approx(90)
    assert result["metrics"]["ambiguous_trades"] == 1
    assert result["metrics"]["hit_rate_pct"] is None
    assert result["metrics"]["net_return_pct"] == pytest.approx(-10)


def test_intrabar_entry_does_not_assume_target_was_reached_after_entry():
    result = simulate(bars=[bar(open=110., low=98., high=125., close=105.)])
    assert result["trades"] == [] and result["statuses"]["one"] == "open"
    assert any(row["code"] == "entry_and_target_same_bar_not_counted_as_win" for row in result["incidents"])
    entry = next(event for event in result["events"] if event["event_type"] == "entry_activated")
    assert entry["intrabar_time_unknown"]
    assert entry["at"] == "2026-11-10T16:01:00+00:00"


def test_ambiguous_first_target_does_not_become_a_verified_later_gap_win():
    result = simulate(bars=[bar(open=110., low=98., high=125., close=105.),
                            bar("11", open=130., low=128., high=132., close=131.)])
    assert len(result["trades"]) == 1
    assert not result["trades"][0]["verified"]
    assert result["metrics"]["verified_closed_trades"] == 0
    assert result["metrics"]["hit_rate_pct"] is None
    assert not result["metrics"]["reliable"]


def test_stop_gap_fills_at_worse_open_and_target_gap_at_actual_open():
    stopped = simulate(bars=[bar(), bar("11", open=80., high=85., low=75., close=82.)])
    assert stopped["trades"][0]["exit_price_native"] == 80.
    assert stopped["cash_eur"] == 80.
    won = simulate(bars=[bar(), bar("11", open=125., high=130., low=123., close=128.)])
    assert won["trades"][0]["exit_price_native"] == 125.
    assert won["cash_eur"] == 125.


def test_sale_cash_is_not_reused_at_an_earlier_opening_of_another_ticker():
    calls = [call("aaa"), call("bbb", "BBB")]
    result = simulate(calls, [bar(), bar(ticker="BBB"), bar("11", high=125.),
                             bar("11", ticker="BBB"), bar("12", ticker="BBB")])
    entries = [event for event in result["events"] if event["event_type"] == "entry_activated"]
    assert [event["ticker"] for event in entries] == ["AAA", "BBB"]
    assert entries[1]["executed_at"].startswith("2026-11-12T10:00")
    assert result["cash_eur"] >= 0
    assert result["nav_eur"] == pytest.approx(
        result["cash_eur"] + sum(position["mark_eur"] for position in result["open_positions"])
    )
    assert result["metrics"]["net_profit_eur"] == pytest.approx(
        result["metrics"]["realized_pnl_eur"] + result["metrics"]["unrealized_pnl_eur"]
    )


def test_fees_spread_and_slippage_reduce_results_and_match_cash_accounting():
    series = [bar(), bar("11", high=125.)]
    theoretical = simulate(bars=series)
    charged = simulate(bars=series, costs=SimulationCosts())
    assert theoretical["cash_eur"] == 120.
    assert charged["cash_eur"] < theoretical["cash_eur"]
    assert charged["metrics"]["total_estimated_costs_eur"] > 2.
    trade = charged["trades"][0]
    assert trade["pnl_eur"] == pytest.approx(trade["exit_proceeds_eur"] - trade["entry_cost_eur"])
    assert charged["cash_eur"] == pytest.approx(100 + trade["pnl_eur"])
    assert charged["metrics"]["max_simultaneously_committed_eur"] <= 100.


@pytest.mark.parametrize("quote", [
    {"currency": "USD"},
    {"currency": "USD", "fx_eur_per_unit": .9, "fx_observed_at": "2026-11-10T11:00:00Z"},
    {"currency": "EUR", "fx_eur_per_unit": .9},
    {"currency": "USD", "fx_eur_per_unit": -1, "fx_observed_at": "2026-11-10T09:00:00Z"},
    {"close": float("nan")}, {"low": -1}, {"high": 90.},
])
def test_bad_quote_or_fx_remains_non_executable(quote):
    recommendation = call(currency="USD" if quote.get("currency") == "USD" else "EUR")
    result = simulate([recommendation], [bar(**quote)])
    assert not result["events"]
    assert result["cash_eur"] == 100
    assert result["metrics"]["net_return_pct"] == 0
    assert any(row["code"] == "invalid_quote_or_historical_fx" for row in result["incidents"])


def test_currency_mismatch_never_silently_substitutes_another_listing():
    quote = bar(currency="USD", fx_eur_per_unit=.9, fx_observed_at="2026-11-10T09:00:00Z")
    result = simulate([call(currency="EUR")], [quote])
    assert result["events"] == []
    assert result["statuses"]["one"] == "not_verifiable"
    assert any(row["code"] == "quote_currency_mismatch" for row in result["incidents"])


def test_known_fx_on_each_leg_is_used_without_current_rate_backfill():
    first = bar(currency="USD", fx_eur_per_unit=.9, fx_observed_at="2026-11-10T09:00:00Z")
    second = bar("11", currency="USD", high=125., fx_eur_per_unit=.8,
                 fx_observed_at="2026-11-11T09:00:00Z")
    result = simulate([call(currency="USD")], [first, second])
    assert result["cash_eur"] == pytest.approx(100 / 90 * 120 * .8)
    assert result["trades"][0]["entry_fx_eur_per_unit"] == .9
    assert result["trades"][0]["exit_fx_eur_per_unit"] == .8


def test_identical_restarts_duplicates_and_input_order_do_not_change_the_ledger():
    one, two = call("a"), call("b", "BBB")
    series = [bar(), bar(ticker="BBB"), bar("11", high=125.)]
    expected = simulate([one, two], series)
    repeated = simulate([two, one, deepcopy(one)], list(reversed(series)) + [deepcopy(series[0])])
    assert repeated == expected
    assert len({event["event_id"] for event in repeated["events"]}) == len(repeated["events"])


def test_conflicting_revisions_or_quotes_do_not_select_a_retrospective_winner():
    result = simulate([call(), call(target=150.)])
    assert result["events"] == [] and result["statuses"]["one"] == "not_verifiable"
    quoted = simulate(bars=[bar(), bar(close=104.)])
    assert quoted["events"] == [] and quoted["coverage"]["usable_price_bars"] == 0
    assert any(row["code"] == "conflicting_quote_rows" for row in quoted["incidents"])


def test_expired_order_cannot_be_filled_after_its_deadline():
    result = simulate([call(expires_at="2026-11-10T09:30:00Z")])
    assert not result["events"] or all(event["event_type"] == "expired" for event in result["events"])
    assert result["statuses"]["one"] == "expired"


def test_whole_share_budget_is_explicit_and_no_leverage_is_created():
    result = simulate(config=SimulationConfig(fractional_shares=False),
                      costs=SimulationCosts(commission_eur=1))
    assert result["events"] == [] and result["cash_eur"] == 100
    assert any(row["code"] == "budget_cannot_buy_one_share" for row in result["incidents"])


def test_analyst_accounts_are_independent_not_added_percentages():
    result = simulate([call("a", analyst="Primero"), call("b", "BBB", analyst="Segundo")],
                      [bar(), bar("11", high=125.), bar(ticker="BBB"), bar("11", ticker="BBB", low=80.)])
    assert set(result["by_analyst"]) == {"Primero", "Segundo"}
    assert result["by_analyst"]["Primero"]["net_return_pct"] == pytest.approx(20)
    assert result["by_analyst"]["Segundo"]["net_return_pct"] == pytest.approx(-10)
    assert result["metrics"]["net_return_pct"] != pytest.approx(10)


def test_four_accounts_have_identical_costs_and_no_synthetic_news_evidence():
    external = call("external", strategy="D")
    technical = call("technical", strategy="C", engine_snapshot_id="frozen-test",
                     signal_known_at="2026-11-10T09:00:00Z")
    no_evidence = call("earnings", strategy="A")
    result = simulate_four_strategies([external, technical, no_evidence], [bar(), bar("11", high=125.)],
                                     as_of=AS_OF, costs=ZERO)
    assert result["total_initial_cash_eur"] == 400.
    accounts = result["accounts"]
    assert accounts["A"]["status"] == accounts["B"]["status"] == "awaiting_evidence"
    assert accounts["A"]["evidence_rejected"] == 1
    assert accounts["C"]["cash_eur"] == accounts["D"]["cash_eur"] == 120.
    assert all(account["initial_cash_eur"] == 100. for account in accounts.values())
    assert not result["is_real_execution"]


def test_news_evidence_and_indirect_relationship_are_point_in_time_not_post_hoc():
    a = call("a", strategy="A", evidence_url="https://issuer.example/earnings",
             evidence_at="2026-11-10T09:00:00Z")
    b = call("b", strategy="B", evidence_url="https://issuer.example/results",
             evidence_at="2026-11-10T09:00:00Z", beneficiary_relation="Proveedor contratado documentado")
    bad = call("bad", strategy="B", evidence_url="https://127.0.0.1/results",
               evidence_at="2026-11-10T09:00:00Z", beneficiary_relation="Not public")
    result = simulate_four_strategies([a, b, bad], [bar()], as_of=AS_OF, costs=ZERO)
    assert result["accounts"]["A"]["status"] == result["accounts"]["B"]["status"] == "simulated"
    assert result["accounts"]["B"]["evidence_rejected"] == 1
    assert len(result["accounts"]["B"]["open_positions"]) == 1


@pytest.mark.parametrize("changes", [{"commission_eur": -1}, {"slippage_bps": float("inf")},
                                     {"spread_bps": True}, {"fx_fee_bps": 1000}])
def test_invalid_costs_are_not_zeroed(changes):
    with pytest.raises(ValueError):
        SimulationCosts(**changes)
