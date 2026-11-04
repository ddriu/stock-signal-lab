from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from src.alert_market_inputs import OwnerAlertMarketInputs, prefer_alert_prices
from src.market_cache import MarketCacheStore, encode_prices


TARGET = date(2026, 11, 3)
CLOCK = "2026-11-04T01:58:00+00:00"


def prices(end="2026-11-03", *, ticker="ANET", currency="USD"):
    index = pd.bdate_range("2025-03-01", end)
    close = pd.Series(range(len(index)), index=index, dtype=float) / 10 + 100
    frame = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                          "close": close, "volume": 1000.0}, index=index)
    frame.attrs = {"ticker": ticker, "quote_currency": currency,
                   "provider": "Synthetic provider", "fetched_at_utc": CLOCK}
    return frame


def row(frame=None, **changes):
    result = {"owner": "synthetic", "kind": "prices", "symbol": "ANET",
              "observed_at": "2026-11-03T21:50:00+00:00",
              "payload": encode_prices(frame if frame is not None else prices()),
              "metadata": {"auto_adjust": True, "interval": "1d", "completed_price_date": "2026-11-03"}}
    result.update(changes)
    return result


def choose(downloaded=None, cached=None, **kwargs):
    return prefer_alert_prices(downloaded if downloaded is not None else prices("2026-11-02"),
        cached if cached is not None else row(), owner="synthetic", ticker="ANET", target_date=TARGET,
        now=CLOCK, **kwargs)


def test_complete_newer_cache_replaces_whole_series_and_preserves_attrs_without_mutation():
    downloaded, cached = prices("2026-11-02"), row()
    before = downloaded.copy(deep=True)
    chosen, saved = choose(downloaded, cached)
    assert saved and chosen.index[-1].date() == TARGET
    assert chosen.index[0].date() >= TARGET - pd.Timedelta(days=550)
    assert chosen.attrs["provider"] == "Synthetic provider"
    assert chosen.attrs["quote_currency"] == "USD"
    assert chosen.attrs["alert_input_origin"] == "owner_market_cache"
    pd.testing.assert_frame_equal(downloaded, before)
    chosen.iloc[-1, 0] = 1
    assert downloaded.iloc[-1, 0] != 1


@pytest.mark.parametrize("cached", [
    row(owner="another-owner"), row(symbol="MA"), row(kind="fundamentals"),
    row(observed_at="2026-11-05T00:00:00Z"), row(observed_at="2026-11-03T20:00:00Z"),
    row(metadata={"auto_adjust": False, "interval": "1d", "completed_price_date": "2026-11-03"}),
    row(metadata={"auto_adjust": True, "interval": "1h", "completed_price_date": "2026-11-03"}),
    row(metadata={"auto_adjust": True, "interval": "1d", "completed_price_date": None}),
    row(payload={"encoding": "invalid"}),
    row(prices(currency="EUR")), row(prices(currency="GBp")),
    row(prices("2026-11-02")),
])
def test_invalid_foreign_partial_old_or_incompatible_cache_does_not_replace_download(cached):
    chosen, saved = choose(cached=cached)
    assert not saved
    assert chosen.index[-1].date() == date(2026, 11, 2)


def test_equal_session_prefers_download_not_different_cached_prices():
    downloaded = prices()
    downloaded.loc[:, ["open", "high", "low", "close"]] *= 2
    chosen, saved = choose(downloaded)
    assert not saved
    assert chosen.close.iloc[-1] == downloaded.close.iloc[-1]


def test_later_read_does_not_certify_intraday_payload_and_future_rows_do_not_leak():
    # A Nov4 bar was saved during its session. It may supply the confirmed
    # Nov3 close for the requested report, never the Nov4 partial quote.
    candidate = row(prices("2026-11-04"), observed_at="2026-11-04T15:00:00Z")
    chosen, saved = prefer_alert_prices(prices("2026-11-02"), candidate, owner="synthetic",
        ticker="ANET", target_date=TARGET, now="2026-11-04T16:00:00Z")
    assert saved and chosen.index[-1].date() == TARGET
    candidate = row(prices(), observed_at="2026-11-03T20:30:00Z")
    chosen, saved = choose(cached=candidate)
    assert not saved and chosen.index[-1].date() == date(2026, 11, 2)


def test_absent_cache_backend_or_database_does_not_create_storage(tmp_path):
    missing = tmp_path / "must-not-be-created.sqlite"
    inputs = OwnerAlertMarketInputs(SimpleNamespace(owner="synthetic", database_path=missing),
        ["ANET"], target_date=TARGET, now=CLOCK)
    assert inputs.select("ANET", prices("2026-11-02"))[1] is False
    assert not missing.exists()
    fake = OwnerAlertMarketInputs(SimpleNamespace(owner="synthetic"), ["ANET"], target_date=TARGET, now=CLOCK)
    assert fake.select("ANET", prices("2026-11-02"))[1] is False


def test_real_store_read_keeps_owner_scope_and_never_inherits_another_owners_prices(tmp_path):
    from src.journal import TradingJournal
    first = TradingJournal(tmp_path / "owners.sqlite", owner="synthetic")
    second = TradingJournal(tmp_path / "owners.sqlite", owner="other")
    cache_row = row()
    MarketCacheStore(first).put("prices", "ANET", cache_row["payload"], cache_row["metadata"],
        observed_at=cache_row["observed_at"])
    cache = OwnerAlertMarketInputs(first, ["ANET"], target_date=TARGET, now=CLOCK)
    assert cache.rows["ANET"]["owner"] == "synthetic"
    assert cache.select("ANET", prices("2026-11-02"))[1] is True
    other = OwnerAlertMarketInputs(second, ["ANET"], target_date=TARGET, now=CLOCK)
    assert other.select("ANET", prices("2026-11-02"))[1] is False
    first.owner = "other"
    assert cache.select("ANET", prices("2026-11-02"))[1] is False


def test_cache_failure_is_optional_and_never_prompts_a_new_backend_or_download(monkeypatch):
    class Unavailable:
        def __init__(self, journal):
            pass
        def read_many(self, *args):
            raise ValueError("Synthetic cache unavailable")
    monkeypatch.setattr("src.alert_market_inputs.MarketCacheStore", Unavailable)
    cache = OwnerAlertMarketInputs(SimpleNamespace(owner="synthetic"), ["ANET"], target_date=TARGET, now=CLOCK)
    assert cache.select("ANET", prices("2026-11-02"))[1] is False


@pytest.mark.parametrize("reference_cached", [True, False])
def test_closing_report_uses_newer_owner_cache_for_us_and_reference_without_backdating(monkeypatch, tmp_path, reference_cached):
    # Real SQLite persistence/transport-cache path, no external data or SMTP.
    from test_alert_runner import _real_closing_digest_case, _verified_growth_info
    from src.alert_runner import run_daily_alerts
    from src.indicators import add_indicators
    from src.email_sender import EmailDeliveryReceipt
    import src.alert_runner as runner

    user, _ = _real_closing_digest_case(monkeypatch, tmp_path, ticker="ANET", session="2026-11-03",
        created_at="2026-11-04T01:55:00+00:00", clock=CLOCK)
    monkeypatch.setattr(user, "list_favorites", lambda: pd.DataFrame({"ticker": ["ANET", "1801.HK"]}))
    monkeypatch.setattr(runner, "add_indicators", add_indicators)
    original_init = runner.OwnerAlertMarketInputs.__init__
    def fixed_clock(self, journal, symbols, **kwargs):
        original_init(self, journal, symbols, **kwargs, now=CLOCK)
    monkeypatch.setattr(runner.OwnerAlertMarketInputs, "__init__", fixed_clock)
    store = MarketCacheStore(user)
    for ticker in (("ANET", "SPY", "XLK") if reference_cached else ("ANET",)):
        source = row(prices(ticker=ticker), symbol=ticker)
        store.put("prices", ticker, source["payload"], source["metadata"], observed_at=source["observed_at"])
    references = []
    real_growth = runner.evaluate_growth_momentum
    def inspect_reference(**kwargs):
        if kwargs["ticker"] == "ANET":
            references.append(kwargs["broad_market"].index[-1].date())
        return real_growth(**kwargs)
    monkeypatch.setattr(runner, "evaluate_growth_momentum", inspect_reference)
    messages = []
    def sender(*args):
        messages.append(args)
        return EmailDeliveryReceipt(status="smtp_accepted", smtp_code=250,
            accepted_at=CLOCK, receipt_hash="a" * 64, message_hash="b" * 64)
    def loader(ticker, *args, **kwargs):
        return prices("2026-11-03" if ticker == "1801.HK" else "2026-11-02", ticker=ticker)
    result = run_daily_alerts(owner="synthetic", journal_factory=lambda owner: user,
        downloader=loader, fundamental_downloader=_verified_growth_info, sender=sender,
        today=TARGET, require_current_session=True)
    assert result.emails_sent == 1 and len(messages) == 1
    assert references == [TARGET if reference_cached else date(2026, 11, 2)]
    stored = user.list_analysis_snapshots()
    assert set(stored.ticker) == {"ANET", "1801.HK"}
    assert set(pd.to_datetime(stored.analyzed_at).dt.date) == {TARGET}
    assert set(pd.to_datetime(stored.created_at, utc=True)) == {pd.Timestamp("2026-11-04T01:55:00+00:00")}
    with user._connect() as connection:
        report_id = connection.execute("SELECT report_id FROM daily_score_reports").fetchone()[0]
    report = user.get_daily_score_report(report_id)
    assert len(report["rows"]) == 2 and all(item["score"] == 85 for item in report["rows"])
    incident = next(item for item in report["incidents"] if item["ticker"] == "ANET")
    assert "cierre guardado más reciente: fecha 2026-11-03" in incident["reason"]
    assert "2026-11-03T21:50:00+00:00" in incident["reason"]
    assert "descarga retrasada" in incident["reason"]
    if not reference_cached:
        assert "referencia general retrasada: cierre 2026-11-02, anterior a la señal 2026-11-03" in incident["reason"]
    # Only the expected private analysis/report/mail ledgers were touched;
    # no trade or transport-cache revision is manufactured by source selection.
    assert user.list_operations().empty
    assert store.heads()[0]["observed_at"] == "2026-11-03T21:50:00.000000000+00:00"


def test_cached_fallback_is_counted_when_download_fails_but_failure_remains_visible(monkeypatch, tmp_path):
    from test_alert_runner import _real_closing_digest_case, _verified_growth_info
    from src.alert_runner import run_daily_alerts
    from src.data_loader import DataDownloadError
    from src.indicators import add_indicators
    from src.email_sender import EmailDeliveryReceipt
    import src.alert_runner as runner

    user, _ = _real_closing_digest_case(monkeypatch, tmp_path, ticker="ANET", session="2026-11-03",
        created_at="2026-11-04T01:55:00+00:00", clock=CLOCK)
    monkeypatch.setattr(runner, "add_indicators", add_indicators)
    original_init = runner.OwnerAlertMarketInputs.__init__
    def fixed_clock(self, journal, symbols, **kwargs):
        original_init(self, journal, symbols, **kwargs, now=CLOCK)
    monkeypatch.setattr(runner.OwnerAlertMarketInputs, "__init__", fixed_clock)
    cached = row()
    MarketCacheStore(user).put("prices", "ANET", cached["payload"], cached["metadata"],
        observed_at=cached["observed_at"])
    def downloader(*args, **kwargs):
        raise DataDownloadError("Synthetic provider unavailable")
    result = run_daily_alerts(owner="synthetic", journal_factory=lambda owner: user,
        downloader=downloader, fundamental_downloader=_verified_growth_info,
        sender=lambda *args: EmailDeliveryReceipt(status="smtp_accepted", smtp_code=250,
            accepted_at=CLOCK, receipt_hash="a" * 64, message_hash="b" * 64),
        today=TARGET, require_current_session=True)
    assert result.emails_sent == result.tickers_with_prices == result.tickers_with_fresh_prices == 1
    assert any("Synthetic provider unavailable" in error for error in result.errors)
    assert len(user.list_analysis_snapshots()) == 1
