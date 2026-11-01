"""Transport-only worker against temporary SQLite and synthetic providers.

No live provider, SMTP, signal engine, hosted database or actual account is
used. Clocks are explicit, so repeats do not depend on the test machine's date.
"""
from copy import deepcopy
from datetime import date
import logging
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

import src.hourly_market_refresh as worker
from src.alerts import AlertPreferences
from src.data_sources import FxSnapshot
from src.hourly_market_schedule import completed_daily_prices
from src.journal import TradingJournal
from src.market_cache import MarketCacheStore, decode_prices
from src.model_results_store import GENESIS_HASH, ModelResultsStore


OPEN = pd.Timestamp("2026-10-30T16:00:00Z")
WEEKEND = pd.Timestamp("2026-10-31T16:00:00Z")


def forbidden(*args, **kwargs):
    raise AssertionError("The cache worker attempted network, SMTP or a signal engine")


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("smtplib.SMTP", forbidden)
    monkeypatch.setattr("smtplib.SMTP_SSL", forbidden)
    monkeypatch.setattr("src.signal_engine.evaluate_latest_signal", forbidden)
    monkeypatch.setattr("src.indicators.add_indicators", forbidden)
    monkeypatch.setattr("src.growth_momentum.evaluate_growth_momentum", forbidden)


@pytest.fixture
def journal(tmp_path):
    return TradingJournal(tmp_path / "private.sqlite", owner="synthetic-owner")


def frame(dates=("2026-10-29", "2026-10-30"), *, currency="USD", provider="Synthetic"):
    count = len(dates)
    result = pd.DataFrame({
        "open": [100.0 + i for i in range(count)],
        "high": [104.0 + i for i in range(count)],
        "low": [98.0 + i for i in range(count)],
        "close": [102.0 + i for i in range(count)],
        "volume": [1000 + i for i in range(count)],
    }, index=pd.DatetimeIndex(dates, name="Date"))
    result.attrs = {"provider": provider, "quote_currency": currency}
    return result


class Providers:
    def __init__(self, *, sector=None, prices=None):
        self.price_calls, self.fundamental_calls, self.fx_calls = [], [], 0
        self.sector, self.frame = sector, prices

    def prices(self, ticker, start, end, *, auto_adjust):
        self.price_calls.append((ticker, start, end, auto_adjust))
        return (self.frame.copy(deep=True) if self.frame is not None else frame())

    def fundamentals(self, ticker):
        self.fundamental_calls.append(ticker)
        return {"currency": "USD", "sector": self.sector, "ticker": ticker}

    def fx(self):
        self.fx_calls += 1
        return FxSnapshot(date(2026, 10, 29), {"EUR": 1.0, "USD": 1.16}, "Synthetic ECB")

    def run(self, journal, *, now=OPEN, clock=None, **kwargs):
        return worker.refresh_market_cache(
            journal, now=now, clock=clock or (lambda: now),
            price_loader=self.prices, fundamental_loader=self.fundamentals,
            fx_loader=self.fx, **kwargs,
        )


def cache_rows(journal):
    store = MarketCacheStore(journal)
    return {(head["kind"], head["symbol"]): store.read(head["kind"], head["symbol"])
            for head in store.heads()}


def original_rows(journal):
    """Snapshot every existing non-cache table, including model/hash ledgers."""
    with sqlite3.connect(journal.database_path) as connection:
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name!='market_data_cache' ORDER BY name"
        )]
        return {name: connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
                for name in names}


def test_hourly_ttl_does_not_repeat_successful_prices_or_daily_fundamentals(journal):
    journal.add_favorite("HALO", "Synthetic holding")
    providers = Providers()
    initial = providers.run(journal)
    assert initial.symbols == 1
    assert initial.prices_saved == 2  # HALO plus SPY
    assert initial.fundamentals_saved == initial.fx_saved == 1
    assert initial.errors == []
    repeated = providers.run(journal, now=OPEN + pd.Timedelta(minutes=59))
    assert repeated.prices_saved == repeated.fundamentals_saved == repeated.fx_saved == 0
    assert repeated.skipped == 2
    assert len(providers.price_calls) == 2
    assert providers.fundamental_calls == ["HALO"]
    hourly = providers.run(journal, now=OPEN + pd.Timedelta(hours=1))
    assert hourly.prices_saved == 2
    assert hourly.fundamentals_saved == hourly.fx_saved == 0
    assert len(providers.price_calls) == 4
    assert all(call[3] is True for call in providers.price_calls)
    assert providers.price_calls[0][1] == date(2021, 10, 1)


def test_weekend_is_inert_but_explicit_bootstrap_can_seed_closed_market(journal):
    journal.add_favorite("HALO")
    providers = Providers()
    idle = providers.run(journal, now=WEEKEND)
    assert idle.prices_saved == idle.fundamentals_saved == idle.fx_saved == 0
    assert not providers.price_calls and not providers.fundamental_calls
    assert MarketCacheStore(journal).heads() == []
    initial = providers.run(journal, now=WEEKEND, bootstrap=True)
    assert initial.prices_saved == 2
    assert initial.fundamentals_saved == initial.fx_saved == 1
    saved = MarketCacheStore(journal).read("prices", "HALO")
    assert saved["metadata"]["completed_price_date"] == "2026-10-30"


def test_fundamentals_rotate_twenty_then_missing_then_oldest_after_twenty_four_hours(journal):
    tickers = [f"T{index:03}" for index in range(25)]
    for ticker in tickers:
        journal.add_favorite(ticker)
    providers = Providers()
    first = providers.run(journal)
    assert first.fundamentals_saved == 20
    assert providers.fundamental_calls == tickers[:20]
    second = providers.run(journal, now=OPEN + pd.Timedelta(minutes=1))
    assert second.fundamentals_saved == 5
    assert providers.fundamental_calls[20:] == tickers[20:]
    providers.run(journal, now=OPEN + pd.Timedelta(hours=23), bootstrap=True)
    assert len(providers.fundamental_calls) == 25
    at_boundary = providers.run(journal, now=OPEN + pd.Timedelta(hours=24), bootstrap=True)
    assert at_boundary.fundamentals_saved == 20
    assert providers.fundamental_calls[25:] == tickers[:20]


def test_existing_fundamentals_are_ordered_by_observed_time_not_favorite_order(journal):
    for ticker in ["NEWER", "OLDEST", "MISSING", "FRESH"]:
        journal.add_favorite(ticker)
    store = MarketCacheStore(journal)
    for ticker, observed in [("NEWER", "2026-10-29T15:00Z"), ("OLDEST", "2026-10-27T15:00Z"),
                             ("FRESH", "2026-10-30T15:00Z")]:
        store.put("fundamentals", ticker, {"currency": "USD"}, {}, observed_at=observed)
    providers = Providers()
    result = providers.run(journal, fundamental_limit=2)
    assert result.fundamentals_saved == 2
    assert providers.fundamental_calls == ["MISSING", "OLDEST"]


def test_provider_failure_preserves_all_last_good_observations_and_hides_logs(journal, capsys):
    journal.add_favorite("HALO")
    Providers().run(journal)
    before = cache_rows(journal)
    private = "private-watchlist-name credential-do-not-print"

    def failing(*args, **kwargs):
        print(private)
        logging.critical(private)
        raise RuntimeError(private)

    result = worker.refresh_market_cache(
        journal, now=OPEN + pd.Timedelta(days=3), clock=lambda: OPEN + pd.Timedelta(days=3),
        price_loader=failing, fundamental_loader=failing, fx_loader=failing,
    )
    assert result.prices_saved == result.fundamentals_saved == result.fx_saved == 0
    assert result.errors and all(private not in value for value in result.errors)
    assert cache_rows(journal) == before
    output = capsys.readouterr()
    assert private not in output.out + output.err


@pytest.mark.parametrize("kind", ["empty", "duplicates", "reversed", "negative", "future"])
def test_invalid_price_history_cannot_replace_last_good_cache(journal, kind):
    journal.add_favorite("HALO")
    Providers().run(journal)
    before = MarketCacheStore(journal).read("prices", "HALO")
    bad = frame()
    if kind == "empty":
        bad = bad.iloc[:0]
    elif kind == "duplicates":
        bad.index = pd.DatetimeIndex(["2026-10-30", "2026-10-30"])
    elif kind == "reversed":
        bad = bad.iloc[::-1]
    elif kind == "negative":
        bad.loc[:, "close"] = -1
    elif kind == "future":
        bad.index = pd.DatetimeIndex(["2026-10-30", "2026-11-02"])
    result = Providers(prices=bad).run(journal, now=OPEN + pd.Timedelta(hours=1))
    assert result.prices_saved == 0
    assert any(error.startswith("prices:") for error in result.errors)
    assert MarketCacheStore(journal).read("prices", "HALO") == before


def test_partial_today_is_indicative_quote_but_not_a_completed_engine_observation(journal):
    journal.add_favorite("HALO")
    raw = frame()
    raw["score"] = 99  # provider-like surplus column cannot become frozen engine data
    Providers(prices=raw).run(journal)
    saved = MarketCacheStore(journal).read("prices", "HALO")
    quote = saved["metadata"]["quote"]
    assert quote["price"] == 103.0 and quote["price_date"] == "2026-10-30"
    assert quote["indicative"] is True
    assert quote["time_precision"] == "session_date_only"
    assert saved["metadata"]["completed_price_date"] == "2026-10-29"
    cached = decode_prices(saved["payload"])
    assert "score" not in cached
    closed = completed_daily_prices("HALO", cached, now=OPEN)
    assert closed.index.tolist() == [pd.Timestamp("2026-10-29")]


def test_known_currency_subunits_are_normalized_in_quote_not_twice_in_transport(journal):
    journal.add_favorite("TEST.L")
    providers = Providers(prices=frame(currency="GBp"))

    def info(ticker):
        return {"currency": "GBp"}

    result = worker.refresh_market_cache(
        journal, now=OPEN, clock=lambda: OPEN, price_loader=providers.prices,
        fundamental_loader=info, fx_loader=providers.fx,
    )
    assert not result.errors
    row = MarketCacheStore(journal).read("prices", "TEST.L")
    assert row["metadata"]["quote"]["price"] == pytest.approx(1.03)
    assert row["metadata"]["quote"]["currency"] == "GBP"
    assert decode_prices(row["payload"]).close.tolist() == [102.0, 103.0]


def test_sector_and_broad_references_are_deduplicated_and_cached(journal):
    journal.add_favorite("HALO")
    journal.add_favorite("SPY")
    providers = Providers(sector="Healthcare")
    result = providers.run(journal)
    symbols = [call[0] for call in providers.price_calls]
    assert set(symbols) == {"HALO", "SPY", "XLV"}
    assert len(symbols) == len(set(symbols))
    assert result.prices_saved == 3
    assert MarketCacheStore(journal).read("prices", "XLV") is not None


def test_foreign_market_reference_uses_its_own_exchange_refresh_window(journal):
    journal.add_favorite("EDEN.PA")
    providers = Providers()
    result = providers.run(journal, now=pd.Timestamp("2026-10-30T10:00Z"))
    assert result.prices_saved == 2  # EDEN and CAC40; US SPY is not open yet
    assert {call[0] for call in providers.price_calls} == {"EDEN.PA", "^FCHI"}


def test_fx_keeps_dated_valid_positive_rates_and_its_own_observation_clock(journal):
    journal.add_favorite("HALO")
    finished = OPEN + pd.Timedelta(seconds=2)
    result = Providers().run(journal, clock=lambda: finished)
    assert result.fx_saved == 1
    fx = MarketCacheStore(journal).read("fx", "ECB")
    assert fx["payload"] == {"as_of": "2026-10-29", "rates_per_eur": {"EUR": 1.0, "USD": 1.16}, "source": "Synthetic ECB"}
    assert pd.Timestamp(fx["observed_at"]) == finished


@pytest.mark.parametrize("bad_fx", [
    FxSnapshot(None, {"EUR": 1.0, "USD": 1.16}),
    FxSnapshot(date(2026, 11, 3), {"EUR": 1.0, "USD": 1.16}),
    FxSnapshot(date(2026, 10, 29), {}),
    FxSnapshot(date(2026, 10, 29), {"EUR": 1.0, "USD": 0.0}),
    FxSnapshot(date(2026, 10, 29), {"EUR": 1.0, "USD": -1.0}),
    FxSnapshot(date(2026, 10, 29), {"EUR": 1.0, "USD": float("nan")}),
])
def test_bad_fx_never_overwrites_last_success(journal, bad_fx):
    journal.add_favorite("HALO")
    providers = Providers()
    providers.run(journal)
    before = MarketCacheStore(journal).read("fx", "ECB")
    result = worker.refresh_market_cache(
        journal, now=OPEN + pd.Timedelta(days=3), clock=lambda: OPEN + pd.Timedelta(days=3),
        price_loader=providers.prices, fundamental_loader=providers.fundamentals,
        fx_loader=lambda: bad_fx,
    )
    assert result.fx_saved == 0
    assert any(error.startswith("fx:") for error in result.errors)
    assert MarketCacheStore(journal).read("fx", "ECB") == before


@pytest.mark.parametrize("payload", [None, [], {"_fundamental_error": "private failure"}])
def test_invalid_fundamentals_preserve_old_cache_without_creating_fallback_scores(journal, payload):
    journal.add_favorite("HALO")
    store = MarketCacheStore(journal)
    store.put("fundamentals", "HALO", {"currency": "USD", "revenueGrowth": 0.2}, {}, observed_at="2026-10-27T16:00Z")
    before = store.read("fundamentals", "HALO")
    providers = Providers()
    result = worker.refresh_market_cache(
        journal, now=OPEN, clock=lambda: OPEN, price_loader=providers.prices,
        fundamental_loader=lambda ticker: payload, fx_loader=providers.fx,
    )
    assert result.fundamentals_saved == 0
    assert any(error.startswith("fundamentals:") for error in result.errors)
    assert store.read("fundamentals", "HALO") == before


def test_rejected_compare_and_swap_is_not_reported_as_saved(journal, monkeypatch):
    journal.add_favorite("HALO")
    original = MarketCacheStore.put

    def rejected(self, kind, *args, **kwargs):
        if kind in {"fundamentals", "fx"}:
            return False
        return original(self, kind, *args, **kwargs)

    monkeypatch.setattr(MarketCacheStore, "put", rejected)
    result = Providers().run(journal)
    assert result.fundamentals_saved == result.fx_saved == 0
    assert MarketCacheStore(journal).read("fundamentals", "HALO") is None
    assert MarketCacheStore(journal).read("fx", "ECB") is None


def test_all_original_real_and_model_tables_are_unchanged(journal, monkeypatch):
    journal.add_operation("HALO", "Compra", 2, 100, 1, "2026-10-01", currency="USD")
    journal.add_favorite("MA")
    journal.upsert_portfolio_snapshot_positions(pd.DataFrame([{
        "snapshot_date": "2026-10-29", "platform": "Synthetic broker",
        "asset_name": "Synthetic holding", "analysis_ticker": "HALO", "value_eur": 200.0,
    }]), recorded_by=journal.owner)
    journal.save_alert_preferences(AlertPreferences(owner=journal.owner, email="synthetic@example.test", enabled=True))
    journal.add_analysis_snapshot(ticker="HALO", analyzed_at="2026-10-29", price=102,
                                 entry_score=88, entry_label="Entrada fuerte", position_label="Mantener",
                                 opportunity_score=70, company_score=70, valuation_score=60,
                                 relative_score=80, risk_score=60, opportunity_label="Candidata")
    ledger = ModelResultsStore(journal)
    season = ledger.create_season({
        "season_key": "cache-test", "strategy_key": "A", "mode": "budget",
        "start_date": "2026-10-29", "base_currency": "USD", "initial_nav_usd": 6000,
        "config": {"budget_usd": 6000}, "git_commit": "c" * 40,
        "engine_version": "model-original-v1",
    })
    ledger.append_batch(
        season["season_id"], run_id="2026-10-29", market_date="2026-10-29",
        events=[{"event_id": "preexisting-observation", "event_type": "observation",
                 "occurred_at": "2026-10-29T20:30:00Z", "available_at": "2026-10-29T20:30:00Z",
                 "payload": {"ticker": "HALO", "action": "hold"}}],
        snapshot={"available_at": "2026-10-29T20:30:00Z", "base_currency": "USD",
                  "cash_usd": 6000, "net_nav_usd": 6000, "benchmark_nav_usd": None,
                  "positions": [], "counts": {"candidates": 0, "filled": 0},
                  "quality_flags": ["benchmark_missing"], "context": {}},
        expected_previous_hash=GENESIS_HASH,
    )
    before = original_rows(journal)
    for name in ("add_operation", "save_alert_preferences", "add_analysis_snapshot",
                 "create_paper_simulation", "upsert_paper_daily_run"):
        monkeypatch.setattr(journal, name, forbidden)
    result = Providers().run(journal)
    assert result.prices_saved > 0
    assert original_rows(journal) == before


@pytest.mark.parametrize("limits", [{"max_symbols": 0}, {"max_symbols": 201},
                                    {"fundamental_limit": -1}, {"fundamental_limit": 26}])
def test_invalid_limits_fail_before_provider_access(journal, limits):
    with pytest.raises(ValueError):
        Providers().run(journal, **limits)


@pytest.mark.parametrize("now", ["2026-10-30T16:00", pd.NaT])
def test_naive_or_missing_reference_time_is_rejected(journal, now):
    with pytest.raises(ValueError):
        Providers().run(journal, now=now)


def test_public_cli_requires_explicit_scope_and_never_prints_owner_or_errors(monkeypatch, capsys):
    import scripts.refresh_market_cache as cli

    monkeypatch.delenv("MARKET_CACHE_OWNER", raising=False)
    monkeypatch.setattr(cli, "load_supabase_config", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "create_journal", forbidden)
    assert cli.main([]) == 1
    assert "no work performed" in capsys.readouterr().out
    owner = "synthetic-private-owner"
    monkeypatch.setenv("MARKET_CACHE_OWNER", owner)
    sentinel = object()
    created, refreshed = [], []
    monkeypatch.setattr(cli, "create_journal", lambda who: created.append(who) or sentinel)

    def refresh(journal, **kwargs):
        refreshed.append((journal, kwargs))
        return worker.RefreshResult(symbols=2, prices_saved=2, errors=["private watchlist error"])

    monkeypatch.setattr(cli, "refresh_market_cache", refresh)
    assert cli.main(["--bootstrap"]) == 0
    assert created == [owner]
    assert refreshed == [(sentinel, {"bootstrap": True})]
    output = capsys.readouterr()
    assert owner not in output.out + output.err
    assert "private watchlist" not in output.out + output.err
    assert "No mail or orders" in output.out


def test_public_cli_exception_is_class_only_and_missing_backend_does_no_work(monkeypatch, capsys):
    import scripts.refresh_market_cache as cli

    monkeypatch.setenv("MARKET_CACHE_OWNER", "synthetic-owner")
    monkeypatch.setattr(cli, "load_supabase_config", lambda: None)
    monkeypatch.setattr(cli, "create_journal", forbidden)
    assert cli.main([]) == 1
    capsys.readouterr()
    monkeypatch.setattr(cli, "load_supabase_config", lambda: object())

    def private_error(owner):
        raise RuntimeError("owner credential endpoint symbol must remain private")

    monkeypatch.setattr(cli, "create_journal", private_error)
    assert cli.main([]) == 1
    output = capsys.readouterr()
    assert output.out.strip() == "Market cache failed: RuntimeError"
    assert not output.err


def test_remote_worker_requests_are_owner_scoped_and_headers_are_not_modified(monkeypatch):
    class Response:
        headers = {}

        def __init__(self, value):
            self.value = value

        def json(self):
            return deepcopy(self.value)

    class Remote:
        owner = "synthetic-owner"
        url = "https://synthetic.invalid"
        headers = {"apikey": "synthetic-key-not-real", "Authorization": "Bearer synthetic-only"}

        def __init__(self):
            self.calls = []

        def list_portfolio_snapshot_positions(self):
            return pd.DataFrame()

        def list_operations(self):
            return pd.DataFrame()

        def list_favorites(self):
            return pd.DataFrame([{"ticker": "HALO"}])

        def _request(self, method, **kwargs):
            self.calls.append((method, kwargs))
            return Response([] if method == "GET" else True)

    journal = Remote()
    before_headers = dict(journal.headers)
    result = Providers().run(journal)
    assert result.prices_saved == 2 and not result.errors
    assert journal.headers == before_headers
    for method, kwargs in journal.calls:
        if method == "GET":
            assert kwargs["params"]["owner"] == "eq.synthetic-owner"
            assert kwargs["headers"]["apikey"] == before_headers["apikey"]
            assert kwargs["headers"]["Authorization"] == before_headers["Authorization"]
        else:
            assert kwargs["json"]["p_owner"] == "synthetic-owner"
            assert "model" not in kwargs["endpoint"] and "operation" not in kwargs["endpoint"]
