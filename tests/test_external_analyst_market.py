"""All market and FX requests are injected; no network/private data in tests."""
import pandas as pd
import pytest

from src.external_analyst_market import download_ecb_history, load_simulation_bars
from src.external_analyst_simulation import SimulationCosts, simulate_external_calls


ACTIVATION = "2026-11-09T08:00:00Z"
AS_OF = "2026-11-11T22:00:00Z"


def call(ticker="AAA", currency="USD", **changes):
    return {"id": ticker, "ticker": ticker, "currency": currency, "provider": "perito_inversor",
            "action": "buy", "direction": "long", "entry_rule": "market", "entry": 100.,
            "stop": 90., "target": 120., "analyst": "Synthetic analyst", "executable": True,
            "received_at": ACTIVATION, "observed_at": ACTIVATION, **changes}


def prices(currency="USD", dates=None):
    frame = pd.DataFrame({"open": [100.] * 3, "high": [105.] * 3,
                          "low": [95.] * 3, "close": [102.] * 3},
                         index=pd.to_datetime(dates or ["2026-11-09", "2026-11-10", "2026-11-11"]))
    frame.attrs.update(quote_currency=currency, corporate_actions_verified=True, split_events={},
                       provider="Synthetic quote provider")
    return frame


def fx(currency="USD", **kwargs):
    return [{"date": day, "currency": currency, "fx_eur_per_unit": .9,
             "fx_observed_at": day + "T17:00:00Z"} for day in ("2026-11-06", "2026-11-09", "2026-11-10")]


def bundle(calls=None, **kwargs):
    return load_simulation_bars(calls or [call()], as_of=kwargs.pop("as_of", AS_OF),
        activation_at=kwargs.pop("activation_at", ACTIVATION),
        downloader=kwargs.pop("downloader", lambda *args, **kwargs: prices()),
        fx_downloader=kwargs.pop("fx_downloader", fx), **kwargs)


def test_usd_data_runs_with_previous_historical_fx_and_actual_exchange_times():
    result = bundle()
    assert result["coverage"]["usable_bars"] == 3
    assert not result["issues"]
    opening = result["bars"][0]
    assert opening["open_at"] == "2026-11-09T14:30:00+00:00"
    assert opening["close_at"] == "2026-11-09T21:00:00+00:00"
    assert opening["available_at"] == "2026-11-09T21:20:00+00:00"
    assert opening["fx_observed_at"].startswith("2026-11-06")
    costs = SimulationCosts(commission_eur=0, spread_bps=0, slippage_bps=0, fx_fee_bps=0)
    run = simulate_external_calls([call()], result["bars"], as_of=AS_OF, costs=costs)
    assert run["open_positions"][0]["entry_at"] == opening["open_at"]
    assert run["nav_eur"] == pytest.approx(102.)


def test_default_frame_can_get_separate_verified_quote_metadata():
    frame = prices()
    frame.attrs.clear()
    seen = []
    def metadata(ticker, **kwargs):
        seen.append((ticker, kwargs))
        return {"currency": "USD", "corporate_actions_verified": True, "split_events": {}}
    result = bundle(downloader=lambda *args, **kwargs: frame, metadata_downloader=metadata)
    assert result["coverage"]["usable_bars"] == 3 and seen[0][0] == "AAA"


def test_euro_does_not_need_or_request_fx():
    def forbidden(*args, **kwargs):
        raise AssertionError("EUR must not request any historical FX")
    result = bundle([call("EDEN.PA", "EUR")], downloader=lambda *args, **kwargs: prices("EUR"),
                    fx_downloader=forbidden)
    assert result["coverage"]["usable_bars"] == 2
    assert result["bars"][0]["fx_eur_per_unit"] == 1
    assert result["bars"][0]["open_at"].endswith("08:00:00+00:00")


def test_fx_is_downloaded_once_per_currency_and_tickers_are_deterministic():
    seen = []
    def download(currency, **kwargs):
        seen.append(currency)
        return fx(currency)
    result = bundle([call("ZZZ"), call("AAA")], fx_downloader=download)
    assert seen == ["USD"] and result["coverage"]["usable_bars"] == 6
    assert [source["ticker"] for source in result["sources"]] == ["AAA", "ZZZ"]


def test_download_has_no_pre_activation_ohlc_or_adjusted_prices():
    seen = []
    def download(ticker, start, end, **kwargs):
        seen.append((start, end, kwargs))
        return prices()
    bundle(downloader=download)
    assert seen[0][0].isoformat() == "2026-11-09"
    assert seen[0][1].isoformat() == "2026-11-11"
    assert seen[0][2] == {"auto_adjust": False}


def test_partial_current_candle_is_not_returned_until_publication_margin():
    result = bundle(as_of="2026-11-11T21:10:00Z")
    assert len(result["bars"]) == 2
    later = bundle(as_of="2026-11-11T21:21:00Z")
    assert len(later["bars"]) == 3


def test_activation_inside_session_cannot_execute_at_earlier_open():
    result = bundle(activation_at="2026-11-09T15:00:00Z")
    assert result["bars"][0]["open_at"].startswith("2026-11-10")


def test_nyse_holiday_and_half_day_use_actual_exchange_schedule():
    frame = prices(dates=["2026-11-25", "2026-11-26", "2026-11-27"])
    result = bundle(activation_at="2026-11-25T08:00:00Z", as_of="2026-11-27T22:00:00Z",
                    downloader=lambda *args, **kwargs: frame, fx_downloader=lambda *args, **kwargs: fx())
    assert [bar["session"] for bar in result["bars"]] == ["2026-11-25", "2026-11-27"]
    assert result["bars"][-1]["close_at"] == "2026-11-27T18:00:00+00:00"


@pytest.mark.parametrize("currency", ["", "GBp", "GBX", "EUR"])
def test_unknown_subunit_or_mismatched_currencies_do_not_get_guessed(currency):
    result = bundle(downloader=lambda *args, **kwargs: prices(currency))
    assert result["coverage"]["usable_bars"] == 0 and result["issues"]


def test_provider_failure_is_partial_and_does_not_expose_raw_error_or_secret():
    def download(ticker, *args, **kwargs):
        if ticker == "AAA":
            raise RuntimeError("https://provider/?api_key=secret")
        return prices()
    result = bundle([call("AAA"), call("BBB")], downloader=download)
    assert result["coverage"]["usable_bars"] == 3
    assert result["issues"][0]["detail"] == "RuntimeError"
    assert "secret" not in str(result)


@pytest.mark.parametrize("alteration,code", [
    ("missing", "provider_missing_session"), ("duplicate", "duplicate_session_quotes"),
    ("negative", "invalid_ohlc"), ("nan", "invalid_ohlc"), ("split", "split_requires_position_adjustment"),
])
def test_invalid_middle_session_has_explicit_barrier_not_silently_bridged(alteration, code):
    frame = prices()
    if alteration == "missing":
        frame = frame.drop(pd.Timestamp("2026-11-10"))
    elif alteration == "duplicate":
        frame = pd.concat([frame, frame.loc[[pd.Timestamp("2026-11-10")]]])
    elif alteration in {"negative", "nan"}:
        frame.loc[pd.Timestamp("2026-11-10"), "low"] = -1 if alteration == "negative" else float("nan")
    else:
        frame.attrs["split_events"] = {"2026-11-10": 2}
    result = bundle(downloader=lambda *args, **kwargs: frame)
    assert any(row["code"] == code for row in result["issues"])
    broken = next(bar for bar in result["bars"] if bar["session"] == "2026-11-10")
    assert broken["data_issue"] == code


def test_new_today_rate_is_not_used_for_earlier_equity_open():
    records = [{"currency": "USD", "fx_eur_per_unit": .9, "fx_observed_at": "2026-11-09T17:00:00Z"}]
    result = bundle(fx_downloader=lambda *args, **kwargs: records)
    assert result["bars"][0]["data_issue"] == "historical_fx_not_known_before_open"
    assert "data_issue" not in result["bars"][1]


def test_conflicting_fx_at_the_same_known_time_does_not_choose_an_input_order_winner():
    rates = fx()
    rates.append({**rates[0], "fx_eur_per_unit": .8})
    forward = bundle(fx_downloader=lambda *args, **kwargs: rates)
    reverse = bundle(fx_downloader=lambda *args, **kwargs: list(reversed(rates)))
    assert forward == reverse
    assert forward["bars"][0]["data_issue"] == "conflicting_historical_fx_observations"
    assert "fx_eur_per_unit" not in forward["bars"][0]


def test_identical_repeated_fx_observation_remains_usable():
    rates = fx()
    rates.append(dict(rates[0]))
    result = bundle(fx_downloader=lambda *args, **kwargs: rates)
    assert result["coverage"]["usable_bars"] == 3


def test_no_today_fx_fallback_when_history_is_missing_or_stale():
    old = [{"currency": "USD", "fx_eur_per_unit": .9, "fx_observed_at": "2026-10-01T16:00:00Z"}]
    result = bundle(fx_downloader=lambda *args, **kwargs: old)
    assert result["coverage"]["usable_bars"] == 0
    assert result["coverage"]["unverifiable_session_markers"] == 3


def test_future_call_and_unknown_calendar_do_not_fetch_prices():
    seen = []
    result = bundle([call("UNSUPPORTED.UNKNOWN"), call("FUTURE", observed_at="2027-01-01T10:00:00Z")],
                    downloader=lambda *args, **kwargs: seen.append(args))
    assert not seen and not result["bars"]
    assert result["issues"][0]["code"] == "unsupported_exchange_calendar"


def test_invalid_activation_or_unbounded_history_fails_before_downloading():
    for activation in ["2026-11-12T08:00:00Z", "2010-01-01T10:00:00Z", "2026-11-09"]:
        with pytest.raises(ValueError):
            bundle(activation_at=activation)


class Response:
    def __init__(self, text):
        self.text, self.content = text, text.encode()
    def raise_for_status(self):
        pass


CSV = "FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,TIME_PERIOD,OBS_VALUE\nD,USD,EUR,SP00,2026-11-09,1.25\nD,USD,EUR,SP00,2026-11-10,1.20\n"


def test_ecb_api_is_bounded_dimension_checked_inverted_and_clocked_brussels():
    seen = []
    def get(url, **kwargs):
        seen.append((url, kwargs))
        return Response(CSV)
    records = download_ecb_history("USD", start="2026-11-09", end="2026-11-10", request_get=get)
    assert records[0]["fx_eur_per_unit"] == .8
    assert records[0]["fx_observed_at"] == "2026-11-09T17:00:00+00:00"
    assert seen[0][0].endswith("D.USD.EUR.SP00.A")
    assert seen[0][1]["timeout"] == (3.05, 15)
    assert seen[0][1]["params"]["endPeriod"] == "2026-11-10"


@pytest.mark.parametrize("csv", [CSV.replace("USD", "GBP"), CSV.replace(",1.25", ",0"),
    CSV.replace("FREQ,", "missing,"), CSV + "D,USD,EUR,SP00,2026-11-09,2\n"])
def test_bad_ecb_dimensions_values_or_conflicting_days_are_not_used(csv):
    with pytest.raises(ValueError):
        download_ecb_history("USD", start="2026-11-09", end="2026-11-10", request_get=lambda *a, **k: Response(csv))
