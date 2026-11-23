"""Slow-provider/cancellation checks with fake time and real local checkpoints.

No provider, remote database, SMTP, or production account is used here. Analysis
and snapshot writes are stubbed; the owner-scoped checkpoint CAS is real SQLite.
"""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd
import pytest

import app
from config import StrategyConfig
from src.journal import TradingJournal
from src.radar_review import BATCH_SIZE, RadarReview
from src.supabase_journal import JournalStorageError


class _ScriptCancelled(BaseException):
    """Streamlit script-control requests do not inherit from Exception."""


@pytest.fixture
def controlled_batch(monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

        @classmethod
        def advance(cls, seconds):
            cls.current += timedelta(seconds=seconds)

    state = {}
    writes = []
    monkeypatch.setattr(app, "datetime", Clock)
    monkeypatch.setattr(app, "st", SimpleNamespace(session_state=state))
    frame = pd.DataFrame({"close": [100.0]}, index=pd.DatetimeIndex(["2026-11-20"]))
    frame.attrs.update(quote_units_normalized=True, display_currency="USD")

    def loaded(tickers, kwargs):
        state["market_data"] = {ticker: frame.copy() for ticker in tickers}
        state["fundamental_data"] = {ticker: {"complete": True} for ticker in tickers}
        for ticker in tickers:
            kwargs["review_reference_data"][app.benchmark_for_ticker(ticker)] = frame.copy()
        return set(tickers)

    def analyze(prices, *_args, **_kwargs):
        rows = [{
            "Ticker": ticker, "Fecha": value.index[-1].date(),
            "Política de análisis": {
                "snapshot_id": sha256(f"synthetic-{ticker}".encode()).hexdigest(),
                "primary": {"data_status": "complete"},
                "comparative": {"data_status": "complete"},
            },
        } for ticker, value in prices.items()]
        return prices, rows, {}, {}, {}, {}, {}

    def persist(_journal, dates, *_args, **_kwargs):
        writes.append(tuple(dates))
        return SimpleNamespace(saved_tickers=tuple(dates), existing_tickers=())

    monkeypatch.setattr(app, "_analysis_view_context", analyze)
    monkeypatch.setattr(app, "persist_analysis_batch_snapshots", persist)
    monkeypatch.setattr(app, "growth_fundamental_status", lambda _info: "complete")
    monkeypatch.setattr(app, "normalized_quote_currency_verified", lambda _frame: True)
    return SimpleNamespace(clock=Clock, state=state, writes=writes, loaded=loaded)


def _start(journal, tickers, clock):
    review = RadarReview.start(tickers, "a" * 64, clock.now(timezone.utc))
    app._save_radar_checkpoint(journal, review)
    return review


def _run(journal, review, worker=None):
    return app._run_radar_batch(
        journal, review, worker or uuid4().hex, history_days=730,
        auto_adjust=True, strategy=StrategyConfig(),
    )


def test_slow_batch_can_finish_after_lease_expiry_without_competing_worker(
    tmp_path, monkeypatch, controlled_batch,
):
    fake = controlled_batch
    journal = TradingJournal(tmp_path / "slow.sqlite", owner="synthetic-owner")
    universe = [f"T{i:03}" for i in range(BATCH_SIZE)]
    review = _start(journal, universe, fake.clock)

    def slow(tickers, *_args, **kwargs):
        claimed = RadarReview.from_checkpoint(journal.get_radar_review_checkpoint())
        # A source can remain busy for longer than the five-minute reservation.
        fake.clock.advance(6 * 60)
        assert not claimed.busy(fake.clock.now(timezone.utc))
        return fake.loaded(tickers, kwargs)

    monkeypatch.setattr(app, "load_market_data", slow)
    completed = _run(journal, review)

    assert not completed.pending
    assert not completed.retryable
    assert completed.worker is None and completed.lease_until is None
    assert fake.writes == [tuple(universe)]
    assert RadarReview.from_checkpoint(journal.get_radar_review_checkpoint()) == completed


def test_expired_slow_worker_cannot_save_snapshots_or_release_successors_claim(
    tmp_path, monkeypatch, controlled_batch,
):
    fake = controlled_batch
    journal = TradingJournal(tmp_path / "takeover.sqlite", owner="synthetic-owner")
    universe = [f"T{i:03}" for i in range(BATCH_SIZE + 1)]
    review = _start(journal, universe, fake.clock)
    next_worker = uuid4().hex
    successor = None

    def slow_then_competing_claim(tickers, *_args, **kwargs):
        nonlocal successor
        fake.clock.advance(6 * 60)
        current = RadarReview.from_checkpoint(journal.get_radar_review_checkpoint())
        successor = current.claim(next_worker, fake.clock.now(timezone.utc))
        app._save_radar_checkpoint(journal, successor)
        return fake.loaded(tickers, kwargs)

    monkeypatch.setattr(app, "load_market_data", slow_then_competing_claim)
    with pytest.raises(JournalStorageError):
        _run(journal, review)

    assert fake.writes == []
    persisted = RadarReview.from_checkpoint(journal.get_radar_review_checkpoint())
    assert persisted == successor
    assert persisted.worker == next_worker
    assert persisted.pending == tuple(universe)
    assert not persisted.outcomes


def test_cancelling_fourth_block_preserves_eighteen_and_resumes_large_universe(
    tmp_path, monkeypatch, controlled_batch,
):
    fake = controlled_batch
    path = tmp_path / "resume-175.sqlite"
    journal = TradingJournal(path, owner="synthetic-owner")
    universe = [f"T{i:03}" for i in range(175)]
    review = _start(journal, universe, fake.clock)
    calls = []
    interrupted = False

    def slow_and_cancel_fourth(tickers, *_args, **kwargs):
        nonlocal interrupted
        calls.append(tuple(tickers))
        # Real time never sleeps: six serial assets each consume fake time.
        fake.clock.advance(15 * len(tickers))
        if len(calls) == 4 and not interrupted:
            interrupted = True
            raise _ScriptCancelled
        return fake.loaded(tickers, kwargs)

    monkeypatch.setattr(app, "load_market_data", slow_and_cancel_fourth)
    for _ in range(3):
        review = _run(journal, review)
    with pytest.raises(_ScriptCancelled):
        _run(journal, review)

    persisted = RadarReview.from_checkpoint(journal.get_radar_review_checkpoint())
    assert len(persisted.outcomes) == 3 * BATCH_SIZE == 18
    assert persisted.pending == tuple(universe[18:])
    assert persisted.worker is None and persisted.lease_until is None
    assert fake.writes == [tuple(universe[start:start + BATCH_SIZE]) for start in (0, 6, 12)]

    # Reopen as a fresh browser/process: no in-memory references survive.
    fake.state.clear()
    reopened = TradingJournal(path, owner="synthetic-owner")
    review = RadarReview.from_checkpoint(reopened.get_radar_review_checkpoint())
    first_resumed_call = len(calls)
    while review.pending:
        review = _run(reopened, review)

    assert calls[first_resumed_call] == tuple(universe[18:24])
    assert [ticker for block in calls[first_resumed_call:] for ticker in block] == universe[18:]
    assert [ticker for block in fake.writes for ticker in block] == universe
    assert len(review.outcomes) == 175
    assert not review.retryable and review.worker is None
    assert RadarReview.from_checkpoint(reopened.get_radar_review_checkpoint()) == review
