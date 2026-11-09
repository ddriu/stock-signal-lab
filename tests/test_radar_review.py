from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd
import pytest

import app
from config import StrategyConfig
from src.journal import TradingJournal
from src.radar_review import BATCH_SIZE, RadarOutcome, RadarReview


def test_state_roundtrips_lease_and_preserves_finished_tickers_on_retry():
    now = datetime(2026, 11, 9, 9, tzinfo=timezone.utc)
    universe = [f"T{i}" for i in range(9)]
    initial = RadarReview.start(universe, "a" * 64, now)
    worker = uuid4().hex
    claimed = initial.claim(worker, now)
    assert RadarReview.from_checkpoint(claimed.checkpoint()) == claimed
    assert claimed.busy(now) and not claimed.busy(now, worker)
    with pytest.raises(ValueError):
        claimed.claim(uuid4().hex, now)
    assert not claimed.busy(now + timedelta(minutes=6))
    with pytest.raises(ValueError):
        claimed.release({"OTHER": RadarOutcome("no_data")})
    results = {
        ticker: RadarOutcome("complete", "complete", "2026-11-06") if i % 2
        else RadarOutcome("no_data")
        for i, ticker in enumerate(claimed.pending[:BATCH_SIZE])
    }
    saved = claimed.release(results)
    assert saved.pending == tuple(universe[BATCH_SIZE:])
    assert saved.retry(now).pending == tuple(
        ticker for ticker in universe if ticker not in results or results[ticker].status != "complete"
    )
    assert saved.compatible("a" * 64, now)
    assert not saved.compatible("a" * 64, now + timedelta(days=1))
    assert not saved.compatible("b" * 64, now)


class _Rerun(BaseException):
    """The same control-flow category as Streamlit's script rerun."""


class _Container:
    def container(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def empty(self):
        pass


def _browser(monkeypatch, state=None):
    def rerun():
        pytest.fail("Radar must not request a full rerun between batches")

    st = SimpleNamespace(
        session_state=dict(state or {}),
        empty=_Container,
        rerun=rerun,
        caption=lambda *_: None,
        info=lambda *_: None,
        warning=lambda *_: None,
    )
    monkeypatch.setattr(app, "st", st)
    monkeypatch.setattr(app, "render_automatic_review_status", lambda *a, **k: None)
    return st.session_state


@pytest.mark.parametrize("count", [2 * BATCH_SIZE + 1, 142])
def test_review_saves_each_batch_survives_reconnect_and_retries_only_missing(tmp_path, monkeypatch, count):
    path = tmp_path / "test_radar.sqlite"
    journal = TradingJournal(path, owner="first")
    universe = [f"T{i:03}" for i in range(count)]
    missing = universe[1]
    calls, stored, price_tokens = [], [], []
    browser = _browser(monkeypatch, {"_force_all_favorite_refresh": True})
    failed = {missing}
    frames = pd.DataFrame({"close": [30.0]}, index=pd.DatetimeIndex(["2026-11-06"]))
    interrupted = False

    def load(tickers, *_args, **kwargs):
        nonlocal interrupted
        if len(calls) == 1 and not interrupted:
            interrupted = True
            raise _Rerun
        calls.append(tuple(tickers))
        price_tokens.append(kwargs["price_refresh_token"])
        good = set(tickers).difference(failed)
        for ticker in good:
            kwargs["review_reference_data"][app.benchmark_for_ticker(ticker)] = frames
        app.st.session_state["market_data"] = {ticker: frames for ticker in good}
        app.st.session_state["fundamental_data"] = {ticker: {"complete": True} for ticker in good}
        return good

    def save(_journal, dates, *_args, **kwargs):
        assert isinstance(kwargs["existing_snapshots"], pd.DataFrame)
        stored.append(tuple(dates))
        return SimpleNamespace(saved_tickers=tuple(dates), existing_tickers=())

    def analyze(prices, *_args, **_kwargs):
        complete_policy = {method: {"data_status": "complete"} for method in ("primary", "comparative")}
        rows = [
            {"Ticker": ticker, "Fecha": frame.index[-1].date(), "Política de análisis": complete_policy}
            for ticker, frame in prices.items()
        ]
        return prices, rows, {}, {}, {}, {}, {}

    monkeypatch.setattr(app, "load_market_data", load)
    monkeypatch.setattr(app, "_analysis_view_context", analyze)
    monkeypatch.setattr(app, "persist_analysis_batch_snapshots", save)
    monkeypatch.setattr(app, "growth_fundamental_status", lambda info: "complete" if info else "pending")

    def step():
        return app._radar_review_step(
            journal, universe, strategy=StrategyConfig(), history_days=730, auto_adjust=True,
        )

    with pytest.raises(_Rerun):
        step()
    first = RadarReview.from_checkpoint(journal.get_radar_review_checkpoint())
    assert len(first.outcomes) == BATCH_SIZE
    assert first.outcomes[missing].status == "no_data"
    assert tuple(stored[0]) == tuple(ticker for ticker in universe[:BATCH_SIZE] if ticker != missing)
    assert not first.worker

    # A new browser has no market/cache data or continuation flag.
    browser = _browser(monkeypatch)
    assert step() == (first, True)
    assert len(calls) == 1
    assert TradingJournal(path, owner="second").get_radar_review_checkpoint() is None
    browser["_radar_review_resume"] = True
    final, _ = step()
    assert not final.pending
    assert final.retryable == (missing,)
    assert [ticker for batch in calls for ticker in batch] == universe
    assert price_tokens[-1] == price_tokens[-2]

    failed.clear()
    browser["_radar_review_resume"] = True
    retried, _ = step()
    assert not retried.retryable
    assert calls[-1] == (missing,)
    assert price_tokens[-1] != price_tokens[-2]
    assert sum(len(batch) for batch in stored) == len(universe)


def test_failed_batch_releases_lease_and_keeps_pending(tmp_path, monkeypatch):
    journal = TradingJournal(tmp_path / "failed.sqlite", owner="owner")
    browser = _browser(monkeypatch, {"_force_all_favorite_refresh": True})

    def offline(*args, **kwargs):
        raise RuntimeError("provider offline")

    monkeypatch.setattr(app, "load_market_data", offline)
    app._radar_review_step(journal, ["ONE"], strategy=StrategyConfig(), history_days=400, auto_adjust=True)
    saved = RadarReview.from_checkpoint(journal.get_radar_review_checkpoint())
    assert saved.pending == ("ONE",)
    assert not saved.outcomes
    assert saved.lease_until is None
    assert "_radar_review_active" not in browser


def test_page_switch_releases_inflight_batch_without_marking_it_complete(tmp_path, monkeypatch):
    journal = TradingJournal(tmp_path / "switched.sqlite", owner="owner")
    _browser(monkeypatch, {"_force_all_favorite_refresh": True})

    def interrupted(*args, **kwargs):
        raise _Rerun

    monkeypatch.setattr(app, "load_market_data", interrupted)
    with pytest.raises(_Rerun):
        app._radar_review_step(
            journal, ["ONE"], strategy=StrategyConfig(), history_days=400, auto_adjust=True,
        )
    review = RadarReview.from_checkpoint(journal.get_radar_review_checkpoint())
    assert review.pending == ("ONE",)
    assert review.lease_until is None
    assert not review.outcomes


def test_reference_is_not_allowed_to_see_future_market_dates():
    reference = pd.DataFrame({"close": [80, 81, 89]},
                             index=pd.date_range("2026-11-04", periods=3))
    scoped = app._reference_through(reference, "2026-11-05")
    assert scoped["close"].tolist() == [80, 81]
    assert reference["close"].tolist() == [80, 81, 89]
