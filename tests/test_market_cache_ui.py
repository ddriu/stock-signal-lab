"""Read-only hydration against temporary SQLite; providers are forbidden."""
from copy import deepcopy
from datetime import date
import json
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

from src.data_loader import FUNDAMENTAL_SNAPSHOT_VERSION
from src.data_sources import FxSnapshot
from src.market_cache import MarketCacheError, MarketCacheStore, encode_prices
import src.market_cache_ui as ui


NOW = pd.Timestamp("2026-11-01T12:00:00Z")
OBSERVED = pd.Timestamp("2026-10-30T22:00:00Z")
NEWER = pd.Timestamp("2026-10-31T22:00:00Z")


def forbidden(*args, **kwargs):
    raise AssertionError("Passive hydration must not call providers, SMTP or an engine")


@pytest.fixture(autouse=True)
def prohibit_external_side_effects(monkeypatch):
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("smtplib.SMTP", forbidden)
    monkeypatch.setattr("smtplib.SMTP_SSL", forbidden)
    monkeypatch.setattr("src.data_loader.download_prices", forbidden)
    monkeypatch.setattr("src.data_loader.download_fundamental_snapshot", forbidden)
    monkeypatch.setattr("src.data_sources.download_ecb_fx_snapshot", forbidden)
    monkeypatch.setattr("src.signal_engine.evaluate_latest_signal", forbidden)
    monkeypatch.setattr("src.indicators.add_indicators", forbidden)
    monkeypatch.setattr("src.growth_momentum.evaluate_growth_momentum", forbidden)


@pytest.fixture
def journal(tmp_path):
    return SimpleNamespace(owner="synthetic-owner", database_path=tmp_path / "cache.sqlite")


def frame(dates=("2026-10-29", "2026-10-30"), *, currency="USD", offset=0):
    size = len(dates)
    result = pd.DataFrame({
        "open": [100. + i + offset for i in range(size)],
        "high": [110. + i + offset for i in range(size)],
        "low": [95. + i + offset for i in range(size)],
        "close": [105. + i + offset for i in range(size)],
        "volume": [1000 + i for i in range(size)],
    }, index=pd.DatetimeIndex(dates, name="Date"))
    result.attrs = {"provider": "Synthetic", "quote_currency": currency,
                    "fetched_at_utc": "2026-10-29T22:00:00Z"}
    return result


def save_prices(journal, ticker="HALO", *, data=None, observed=OBSERVED,
                auto_adjust=True, quote=None, completed_price_date=None):
    data = frame() if data is None else data
    metadata = {"auto_adjust": auto_adjust, "interval": "1d",
        "history_start": data.index[0].isoformat(),
        "history_end": data.index[-1].isoformat(), "rows": len(data),
        "completed_price_date": completed_price_date or data.index[-1].date().isoformat(),
        "quote": quote or {"price": float(data.close.iloc[-1]),
            "price_date": data.index[-1].date().isoformat(),
            "currency": data.attrs.get("display_currency") or data.attrs["quote_currency"],
            "indicative": True, "time_precision": "session_date_only", "source": "Synthetic"}}
    assert MarketCacheStore(journal).put("prices", ticker, encode_prices(data), metadata, observed_at=observed)


def save_fundamentals(journal, ticker="HALO", *, version=FUNDAMENTAL_SNAPSHOT_VERSION,
                      payload=None, observed=OBSERVED):
    assert MarketCacheStore(journal).put("fundamentals", ticker,
        payload or {"symbol": ticker, "currency": "USD", "sector": "Healthcare",
                    "_snapshot_version": version},
        {"source_version": version, "currency": "USD", "sector": "Healthcare"},
        observed_at=observed)


def save_fx(journal, *, as_of="2026-10-29", rates=None):
    assert MarketCacheStore(journal).put("fx", "ECB", {
        "as_of": as_of, "rates_per_eur": rates or {"EUR": 1., "USD": 1.16},
        "source": "Synthetic ECB"}, {}, observed_at=OBSERVED)


def force_manifest_refresh(state):
    state["_persistent_market_reads"]["checked"] = -float("inf")


def test_home_reads_only_quote_headers_not_ohlc_or_fundamental_payloads(journal, monkeypatch):
    save_prices(journal)
    save_fundamentals(journal)
    # An unreadable blob proves Home does not even parse the history payload.
    with sqlite3.connect(journal.database_path) as connection:
        connection.execute("UPDATE market_data_cache SET payload='corrupt history' WHERE kind='prices'")
    monkeypatch.setattr(ui, "decode_prices", forbidden)
    monkeypatch.setattr(MarketCacheStore, "read_many", forbidden)
    state = {}
    result = ui.hydrate_saved_market(journal, state, tickers=["HALO"], now=NOW)
    assert result["status"] == "saved" and result["quotes"] == 1
    assert state["_cached_market_quotes"]["HALO"]["metadata"]["quote"]["price"] == 106.
    assert not state.get("market_data") and not state.get("fundamental_data")
    assert state.get("_market_view_revision", 0) == 0


def test_history_is_batched_on_demand_and_warm_rerun_never_rereads(journal, monkeypatch):
    save_prices(journal)
    save_prices(journal, "SPY")
    save_prices(journal, "XLV")
    save_fundamentals(journal)
    save_fx(journal)
    calls = []
    original = MarketCacheStore.read_many

    def spy(store, kind, symbols):
        calls.append((kind, tuple(symbols)))
        return original(store, kind, symbols)

    monkeypatch.setattr(MarketCacheStore, "read_many", spy)
    state = {}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=False, now=NOW)
    assert calls == [("fx", ("ECB",))]
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)
    assert "HALO" in state["market_data"]
    assert set(state["reference_data"]) == {"SPY", "XLV"}
    assert state["fundamental_data"]["HALO"]["currency"] == "USD"
    assert state["fx_snapshot"].rates_per_eur["USD"] == 1.16
    count, revision = len(calls), state["_market_view_revision"]
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)
    assert len(calls) == count and state["_market_view_revision"] == revision


def test_partial_daily_bar_excluded_from_engine_but_quote_available_for_valuation(journal):
    intraday = pd.Timestamp("2026-10-30T16:00:00Z")
    save_prices(journal, observed=intraday - pd.Timedelta(minutes=1))
    state = {}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=intraday)
    assert state["market_data"]["HALO"].index.tolist() == [pd.Timestamp("2026-10-29")]
    before = deepcopy(state["market_data"]["HALO"])
    valued = ui.valuation_frames(state["market_data"], state["_cached_market_quotes"], now=intraday)
    assert valued["HALO"].close.iloc[-1] == 106.
    assert valued["HALO"].attrs["indicative_valuation_only"] is True
    pd.testing.assert_frame_equal(state["market_data"]["HALO"], before)


def test_saved_intraday_bar_is_not_promoted_when_read_after_close(journal):
    save_prices(journal, observed=pd.Timestamp("2026-10-30T16:00:00Z"))
    state = {}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)

    assert state["market_data"]["HALO"].index.tolist() == [pd.Timestamp("2026-10-29")]


def test_hydration_obeys_collector_declared_completed_date(journal):
    save_prices(journal, completed_price_date="2026-10-29")
    state = {}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)

    assert state["market_data"]["HALO"].index.tolist() == [pd.Timestamp("2026-10-29")]


def test_non_adjusted_history_does_not_mix_with_adjusted_engine(journal):
    save_prices(journal, auto_adjust=False)
    state = {}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)
    assert not state.get("market_data")


def test_subunit_attrs_survive_without_changing_raw_history_twice(journal):
    raw = frame(currency="GBp")
    raw.attrs.update(source_currency="GBp", display_currency="GBP",
                     price_scale=.01, quote_units_normalized=False)
    save_prices(journal, "TEST.L", data=raw,
        quote={"price": 1.06, "price_date": "2026-10-30", "currency": "GBP"})
    state = {}
    ui.hydrate_saved_market(journal, state, tickers=["TEST.L"], history=True, now=NOW)
    restored = state["market_data"]["TEST.L"]
    assert restored.close.tolist() == raw.close.tolist()
    for key, expected in raw.attrs.items():
        assert restored.attrs[key] == expected


def test_corrected_unit_attrs_invalidate_analysis_even_when_numbers_identical(journal):
    raw = frame(currency="GBP")
    raw.attrs.update(source_currency="GBp", display_currency="GBP", price_scale=.01,
                     quote_units_normalized=False)
    save_prices(journal, "TEST.L", data=raw)
    state = {}
    ui.hydrate_saved_market(journal, state, tickers=["TEST.L"], history=True, now=NOW)
    revision = state["_market_view_revision"]
    corrected = raw.copy(deep=True)
    corrected.attrs["quote_units_normalized"] = True
    save_prices(journal, "TEST.L", data=corrected, observed=NEWER)
    force_manifest_refresh(state)
    ui.hydrate_saved_market(journal, state, tickers=["TEST.L"], history=True, now=NOW)
    assert state["market_data"]["TEST.L"].attrs["quote_units_normalized"] is True
    assert state["_market_view_revision"] > revision


def test_split_adjustment_correction_replaces_history(journal):
    save_prices(journal)
    state = {}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)
    revision = state["_market_view_revision"]
    corrected = frame()
    for column in ("open", "high", "low", "close"):
        corrected[column] /= 2
    save_prices(journal, data=corrected, observed=NEWER)
    force_manifest_refresh(state)
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)
    assert state["market_data"]["HALO"].close.iloc[-1] == 53.
    assert state["_market_view_revision"] > revision


def test_cache_failure_preserves_previous_market_and_revision(journal, monkeypatch):
    before = frame(offset=-20)
    state = {"market_data": {"HALO": before}, "_market_view_revision": 5}
    monkeypatch.setattr(MarketCacheStore, "heads", lambda *a, **k: (_ for _ in ()).throw(MarketCacheError("offline")))
    result = ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)
    assert result["status"] == "unavailable"
    pd.testing.assert_frame_equal(state["market_data"]["HALO"], before)
    assert state["_market_view_revision"] == 5


def test_decode_failure_does_not_mark_uncommitted_histories_as_already_applied(journal):
    save_prices(journal, "AAPL")
    save_prices(journal, "HALO")
    with sqlite3.connect(journal.database_path) as connection:
        original = json.loads(connection.execute("SELECT payload FROM market_data_cache WHERE symbol='HALO'").fetchone()[0])
        original["sha256"] = "0" * 64
        connection.execute("UPDATE market_data_cache SET payload=? WHERE symbol='HALO'", [json.dumps(original)])
    state = {"market_data": {"AAPL": frame(offset=-20)}, "_market_view_revision": 4}
    failed = ui.hydrate_saved_market(journal, state, tickers=["AAPL", "HALO"], history=True, now=NOW)
    assert failed["status"] == "unavailable"
    assert state["market_data"]["AAPL"].close.iloc[-1] == 86.
    save_prices(journal, "HALO", observed=NEWER)
    force_manifest_refresh(state)
    result = ui.hydrate_saved_market(journal, state, tickers=["AAPL", "HALO"], history=True, now=NOW)
    assert result["status"] == "saved"
    assert state["market_data"]["AAPL"].close.iloc[-1] == 106.
    assert "HALO" in state["market_data"]


def test_separate_owners_do_not_share_quotes_histories_or_fundamentals(journal):
    other = SimpleNamespace(owner="other-owner", database_path=journal.database_path)
    save_prices(journal)
    save_fundamentals(journal)
    first, second = {}, {}
    ui.hydrate_saved_market(journal, first, tickers=["HALO"], history=True, now=NOW)
    ui.hydrate_saved_market(other, second, tickers=["HALO"], history=True, now=NOW)
    assert "HALO" in first["market_data"]
    assert not second.get("market_data") and not second.get("fundamental_data")
    assert not second.get("_cached_market_quotes")


def test_owner_switch_in_same_state_does_not_retain_previous_private_context(journal):
    other = SimpleNamespace(owner="other-owner", database_path=journal.database_path)
    save_prices(journal)
    save_fundamentals(journal)
    state = {}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)
    ui.hydrate_saved_market(other, state, tickers=["HALO"], history=True, now=NOW)
    assert "HALO" not in state.get("market_data", {})
    assert "HALO" not in state.get("fundamental_data", {})
    assert not state.get("_cached_market_quotes")


def test_newer_manual_refresh_wins_over_older_saved_history_and_quote(journal):
    save_prices(journal)
    manual = frame(offset=30)
    manual.attrs["fetched_at_utc"] = "2026-10-31T22:30:00Z"
    state = {"market_data": {"HALO": manual}, "_market_view_revision": 7,
        "_last_price_refresh": {"succeeded": ["HALO"], "requested_at_utc": "2026-10-31T22:30:00Z"}}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)
    pd.testing.assert_frame_equal(state["market_data"]["HALO"], manual)
    assert state["_market_view_revision"] == 7
    valued = ui.valuation_frames(state["market_data"], state["_cached_market_quotes"], now=NOW)
    pd.testing.assert_frame_equal(valued["HALO"], manual)


def test_earlier_radar_batch_keeps_newer_prices_and_fundamentals(journal):
    save_prices(journal)
    save_fundamentals(journal)
    manual = frame(offset=30)
    manual.attrs["fetched_at_utc"] = "2026-10-31T22:30:00Z"
    manual_fundamental = {"symbol": "HALO", "sector": "Healthcare", "marketCap": 99,
                          "_ui_observed_at_utc": "2026-10-31T22:30:01Z"}
    state = {
        "market_data": {"HALO": manual},
        "fundamental_data": {"HALO": manual_fundamental},
        "_market_view_revision": 7,
        # The latest completed batch no longer includes HALO.
        "_last_price_refresh": {"succeeded": ["AAPL"], "requested_at_utc": "2026-10-31T22:50:00Z"},
    }
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)

    pd.testing.assert_frame_equal(state["market_data"]["HALO"], manual)
    assert state["fundamental_data"]["HALO"] == manual_fundamental
    assert state["_market_view_revision"] == 7


def test_later_cache_retrieval_does_not_regress_completed_dates(journal):
    old = frame(dates=("2026-10-28", "2026-10-29"), offset=-50)
    save_prices(journal, data=old, observed=NEWER)
    save_prices(journal, "SPY", data=old, observed=NEWER)
    manual = frame(offset=30)
    state = {"market_data": {"HALO": manual}, "reference_data": {"SPY": manual}}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)

    pd.testing.assert_frame_equal(state["market_data"]["HALO"], manual)
    pd.testing.assert_frame_equal(state["reference_data"]["SPY"], manual)


def test_newer_cache_can_replace_manual_even_when_payload_is_unchanged(journal):
    save_prices(journal)
    save_fundamentals(journal)
    state = {}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)
    manual = frame(offset=30)
    manual.attrs["fetched_at_utc"] = "2026-10-31T16:00:00Z"
    state["market_data"]["HALO"] = manual
    state["fundamental_data"]["HALO"] = {
        "symbol": "HALO", "marketCap": 99, "_ui_observed_at_utc": "2026-10-31T16:00:00Z",
    }
    # Same cached data as before the manual run, but a new verified retrieval.
    save_prices(journal, observed=NEWER)
    save_fundamentals(journal, observed=NEWER)
    force_manifest_refresh(state)
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)

    assert state["market_data"]["HALO"].close.iloc[-1] == 106.
    assert "_ui_observed_at_utc" not in state["fundamental_data"]["HALO"]
    assert "marketCap" not in state["fundamental_data"]["HALO"]


def test_newer_manual_history_is_still_cropped_to_requested_window(journal):
    save_prices(journal)
    manual = frame(offset=30)
    manual.attrs["fetched_at_utc"] = "2026-10-31T22:30:00Z"
    state = {"market_data": {"HALO": manual}}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW,
                           start=date(2026, 10, 30), end=date(2026, 10, 30))

    assert state["market_data"]["HALO"].close.tolist() == [136.]
    assert state["market_data"]["HALO"].index.tolist() == [pd.Timestamp("2026-10-30")]


def test_future_observation_does_not_replace_current_session(journal):
    save_prices(journal, observed=NOW + pd.Timedelta(hours=1))
    previous = frame(offset=-10)
    state = {"market_data": {"HALO": previous}, "_market_view_revision": 5}
    result = ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)
    assert result["status"] == "unavailable"
    pd.testing.assert_frame_equal(state["market_data"]["HALO"], previous)
    assert state["_market_view_revision"] == 5


def test_future_row_written_between_manifest_and_batch_read_is_not_accepted(journal, monkeypatch):
    save_prices(journal)
    original = MarketCacheStore.heads

    def racing_manifest(store, *args, **kwargs):
        heads = original(store, *args, **kwargs)
        save_prices(journal, data=frame(offset=30), observed=NOW + pd.Timedelta(hours=1))
        return heads

    monkeypatch.setattr(MarketCacheStore, "heads", racing_manifest)
    previous = frame(offset=-10)
    state = {"market_data": {"HALO": previous}, "_market_view_revision": 5}
    result = ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)
    assert result["status"] == "unavailable"
    pd.testing.assert_frame_equal(state["market_data"]["HALO"], previous)
    assert state["_market_view_revision"] == 5


@pytest.mark.parametrize("as_of,rates", [
    ("2026-11-02", {"EUR": 1., "USD": 1.16}),
    ("2026-10-29", {"EUR": 2., "USD": 1.16}),
    ("2026-10-29", {"EUR": 1., "USD": 0}),
    ("2026-10-29", {"EUR": 1., "USD": -1.16}),
])
def test_invalid_fx_never_replaces_previous_snapshot(journal, as_of, rates):
    save_fx(journal, as_of=as_of, rates=rates)
    previous = FxSnapshot(date(2026, 10, 28), {"EUR": 1., "USD": 1.15})
    state = {"fx_snapshot": previous}
    ui.hydrate_saved_market(journal, state, tickers=[], now=NOW)
    assert state["fx_snapshot"] == previous


def test_fx_string_rates_are_either_rejected_or_stored_as_finite_numbers(journal):
    save_fx(journal, rates={"EUR": 1., "USD": "1.16"})
    state = {}
    ui.hydrate_saved_market(journal, state, tickers=[], now=NOW)
    if "fx_snapshot" in state:
        assert all(isinstance(value, (int, float)) for value in state["fx_snapshot"].rates_per_eur.values())


def test_requested_start_end_crop_history_without_mutating_stored_payload(journal):
    dates = ("2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30")
    save_prices(journal, data=frame(dates))
    state = {}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW,
        start=date(2026, 10, 28), end=date(2026, 10, 29))
    assert state["market_data"]["HALO"].index.tolist() == [pd.Timestamp("2026-10-28"), pd.Timestamp("2026-10-29")]
    payload = MarketCacheStore(journal).read("prices", "HALO")["payload"]
    assert len(ui.decode_prices(payload)) == 4


def test_window_change_reuses_cached_payload_but_recrops_the_analysis(journal, monkeypatch):
    save_prices(journal)
    state = {}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW,
        start=date(2026, 10, 29), end=date(2026, 10, 30))
    revision = state["_market_view_revision"]
    monkeypatch.setattr(MarketCacheStore, "read_many", forbidden)
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW,
        start=date(2026, 10, 30), end=date(2026, 10, 30))
    assert state["market_data"]["HALO"].index.tolist() == [pd.Timestamp("2026-10-30")]
    assert state["_market_view_revision"] > revision


def test_empty_requested_window_does_not_keep_earlier_out_of_window_history(journal):
    save_prices(journal)
    state = {}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW,
        start=date(2026, 10, 29), end=date(2026, 10, 30))
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW,
        start=date(2026, 10, 20), end=date(2026, 10, 21))
    assert "HALO" not in state.get("market_data", {}) or state["market_data"]["HALO"].empty


def test_intraday_quote_update_does_not_recalculate_unchanged_completed_history(journal):
    save_prices(journal)
    state = {}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)
    revision = state["_market_view_revision"]
    save_prices(journal, observed=NEWER,
        quote={"price": 110., "currency": "USD", "price_date": "2026-10-30"})
    force_manifest_refresh(state)
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)
    assert state["_market_view_revision"] == revision
    valued = ui.valuation_frames(state["market_data"], state["_cached_market_quotes"], now=NOW)
    assert valued["HALO"].close.iloc[-1] == 110.
    assert state["market_data"]["HALO"].close.iloc[-1] == 106.


def test_wrong_fundamental_source_version_skipped(journal):
    save_prices(journal)
    save_fundamentals(journal, version="obsolete-v0")
    previous = {"symbol": "HALO", "currency": "USD", "_snapshot_version": FUNDAMENTAL_SNAPSHOT_VERSION}
    state = {"fundamental_data": {"HALO": previous}}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)
    assert state["fundamental_data"]["HALO"] == previous


def test_payload_fundamental_version_cannot_disagree_with_current_metadata(journal):
    save_prices(journal)
    save_fundamentals(journal, payload={"symbol": "HALO", "currency": "USD", "_snapshot_version": "obsolete-v0"})
    state = {}
    ui.hydrate_saved_market(journal, state, tickers=["HALO"], history=True, now=NOW)
    assert "HALO" not in state.get("fundamental_data", {})


@pytest.mark.parametrize("currency", ["GBp", "ZAc", "ILA"])
def test_unscaled_subunit_quote_not_falsely_marked_normalized_without_history(currency):
    quotes = {"TEST": {"observed_at": OBSERVED.isoformat(), "metadata": {"quote": {
        "price": 106., "currency": currency, "price_date": "2026-10-30"}}}}
    valued = ui.valuation_frames({}, quotes, now=NOW)
    if "TEST" in valued:
        assert valued["TEST"].attrs["display_currency"] in {"GBP", "ZAR", "ILS"}
        assert valued["TEST"].close.iloc[-1] == pytest.approx(1.06)


@pytest.mark.parametrize("quote,observed", [
    ({"price": 110., "currency": "EUR", "price_date": "2026-10-30"}, OBSERVED),
    ({"price": 110., "currency": "USD", "price_date": "2026-11-02"}, OBSERVED),
    ({"price": 110., "currency": "USD", "price_date": "2026-10-30"}, NOW + pd.Timedelta(hours=1)),
    ({"price": 0., "currency": "USD", "price_date": "2026-10-30"}, OBSERVED),
])
def test_invalid_or_mismatched_quote_never_replaces_known_history(quote, observed):
    previous = frame()
    previous.attrs["display_currency"] = "USD"
    result = ui.valuation_frames({"HALO": previous}, {"HALO": {
        "observed_at": observed.isoformat(), "metadata": {"quote": quote}}}, now=NOW)
    pd.testing.assert_frame_equal(result["HALO"], previous)


def poll_state(journal):
    """Existing hydration bucket, without invoking the new poll helper."""
    return {"_persistent_market_reads": {"owner": journal.owner, "checked": 50.,
        "heads": MarketCacheStore(journal).heads(), "rows": {}, "fingerprints": {}}}


def test_poll_initial_and_navigation_within_one_hour_do_no_io(journal, monkeypatch):
    save_prices(journal)
    state = poll_state(journal)
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(MarketCacheStore, "heads", spy)
    monkeypatch.setattr(MarketCacheStore, "read_many", forbidden)
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=100.) is False
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=101.) is False
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=3699.) is False
    assert calls == []


def test_poll_unchanged_heads_after_hour_checks_once_without_rerun(journal, monkeypatch):
    save_prices(journal)
    state = poll_state(journal)
    calls, original = [], MarketCacheStore.heads

    def spy(store, *args, **kwargs):
        calls.append(store.owner)
        return original(store, *args, **kwargs)

    monkeypatch.setattr(MarketCacheStore, "heads", spy)
    monkeypatch.setattr(MarketCacheStore, "read_many", forbidden)
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=100.) is False
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=3700.) is False
    assert calls == [journal.owner]
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=7299.) is False
    assert calls == [journal.owner]
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=7300.) is False
    assert calls == [journal.owner, journal.owner]


def test_poll_new_observation_updates_headers_only_and_requests_one_rerun(journal, monkeypatch):
    save_prices(journal)
    state = poll_state(journal)
    before = frame(offset=-10)
    state.update(market_data={"HALO": before}, _market_view_revision=8)
    original_checked = state["_persistent_market_reads"]["checked"]
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=100.) is False
    save_prices(journal, observed=NEWER)
    monkeypatch.setattr(MarketCacheStore, "read_many", forbidden)
    monkeypatch.setattr(ui, "decode_prices", forbidden)
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=3700.) is True
    bucket = state["_persistent_market_reads"]
    assert bucket["heads"][0]["observed_at"] == NEWER.isoformat(timespec="nanoseconds")
    assert bucket["checked"] > original_checked
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=3701.) is False
    pd.testing.assert_frame_equal(state["market_data"]["HALO"], before)
    assert state["_market_view_revision"] == 8


@pytest.mark.parametrize("change", ["add", "remove"])
def test_poll_detects_header_membership_changes(journal, change):
    save_prices(journal)
    state = poll_state(journal)
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=100.) is False
    if change == "add":
        save_prices(journal, "AAPL")
    else:
        with sqlite3.connect(journal.database_path) as connection:
            connection.execute("DELETE FROM market_data_cache WHERE owner=? AND kind='prices' AND symbol='HALO'", [journal.owner])
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=3700.) is True
    symbols = [head["symbol"] for head in state["_persistent_market_reads"]["heads"]]
    assert symbols == (["AAPL", "HALO"] if change == "add" else [])


def test_poll_ignores_another_owners_updates(journal):
    save_prices(journal)
    state = poll_state(journal)
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=100.) is False
    other = SimpleNamespace(owner="other-owner", database_path=journal.database_path)
    save_prices(other, "AAPL", observed=NEWER)
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=3700.) is False
    assert [head["symbol"] for head in state["_persistent_market_reads"]["heads"]] == ["HALO"]


def test_poll_error_returns_false_preserving_existing_header_cache(journal, monkeypatch):
    save_prices(journal)
    state = poll_state(journal)
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=100.) is False
    before = deepcopy(state["_persistent_market_reads"])
    calls = []

    def failing(*args, **kwargs):
        calls.append(True)
        raise MarketCacheError("synthetic backend unavailable")

    monkeypatch.setattr(MarketCacheStore, "heads", failing)
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=3700.) is False
    assert calls == [True]
    assert state["_persistent_market_reads"] == before


def test_poll_owner_change_initializes_new_marker_without_io(journal, monkeypatch):
    save_prices(journal)
    other = SimpleNamespace(owner="other-owner", database_path=journal.database_path)
    save_prices(other, "AAPL")
    state = poll_state(journal)
    assert ui.poll_saved_market_revision(journal, state, monotonic_now=100.) is False
    calls, original = [], MarketCacheStore.heads

    def spy(store, *args, **kwargs):
        calls.append(store.owner)
        return original(store, *args, **kwargs)

    monkeypatch.setattr(MarketCacheStore, "heads", spy)
    assert ui.poll_saved_market_revision(other, state, monotonic_now=101.) is False
    assert calls == []
    assert ui.poll_saved_market_revision(other, state, monotonic_now=3700.) is False
    assert calls == []
    assert ui.poll_saved_market_revision(other, state, monotonic_now=3701.) is True
    assert calls == [other.owner]
    assert state["_persistent_market_reads"]["owner"] == other.owner
    assert [head["symbol"] for head in state["_persistent_market_reads"]["heads"]] == ["AAPL"]
