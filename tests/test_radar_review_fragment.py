"""Offline scheduling checks, not a substitute for browser DOM validation."""

from datetime import datetime, timezone
import inspect
from types import SimpleNamespace

import pytest

import app
from config import StrategyConfig
from src.journal import TradingJournal
from src.radar_review import BATCH_SIZE, RadarOutcome, RadarReview


class _Slot:
    def empty(self):
        pass

    def container(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class _FullRerun(BaseException):
    pass


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    state, batches, statuses = {}, [], []
    journal = TradingJournal(tmp_path / "fragment.sqlite", owner="synthetic-qa")

    def rerun():
        raise _FullRerun

    monkeypatch.setattr(app, "st", SimpleNamespace(
        session_state=state, empty=_Slot, rerun=rerun,
        info=lambda *_: None, caption=lambda *_: None, warning=lambda *_: None,
    ))
    monkeypatch.setattr(app, "render_automatic_review_status",
                        lambda review, **kwargs: statuses.append((review, kwargs)))

    def batch(journal, review, worker, **kwargs):
        # Actual CAS/checkpoint transitions; market I/O and calculation are fake.
        claimed = review.claim(worker, datetime.now(timezone.utc))
        app._save_radar_checkpoint(journal, claimed)
        pending = claimed.pending[:BATCH_SIZE]
        batches.append(pending)
        kwargs["progress_callback"](pending[0], "Proveedor simulado")
        committed = claimed.release({ticker: RadarOutcome("no_data") for ticker in pending})
        app._save_radar_checkpoint(journal, committed)
        return committed

    monkeypatch.setattr(app, "_run_radar_batch", batch)

    def step(universe, **kwargs):
        return app._radar_review_step(journal, universe, strategy=StrategyConfig(),
                                      history_days=730, auto_adjust=True, **kwargs)

    def start(count):
        universe = [f"QA{i:03}" for i in range(count)]
        state["_force_all_favorite_refresh"] = True
        review, compatible = step(universe, max_batches=0)
        assert compatible
        return universe, review

    return SimpleNamespace(state=state, batches=batches, statuses=statuses,
                           journal=journal, step=step, start=start)


def test_initial_explicit_start_saves_queue_without_a_provider_call(scheduler):
    universe, review = scheduler.start(175)
    assert review.pending == tuple(universe)
    assert not review.outcomes and not scheduler.batches
    assert RadarReview.from_checkpoint(scheduler.journal.get_radar_review_checkpoint()) == review
    assert "_force_all_favorite_refresh" not in scheduler.state


def test_continuation_processes_only_one_durable_block_per_tick(scheduler):
    universe, _ = scheduler.start(175)
    review, compatible = scheduler.step(universe, continue_review=True,
                                        max_batches=1, progress_slot=_Slot())
    assert compatible and scheduler.batches == [tuple(universe[:BATCH_SIZE])]
    assert review.pending == tuple(universe[BATCH_SIZE:])
    assert review.worker is None and review.lease_until is None
    assert RadarReview.from_checkpoint(scheduler.journal.get_radar_review_checkpoint()) == review


def test_passive_navigation_does_not_continue_an_existing_queue(scheduler):
    universe, initial = scheduler.start(13)
    assert scheduler.step(universe) == (initial, True)
    assert scheduler.step(universe, allow_work=False) == (initial, True)
    assert not scheduler.batches


def test_fragment_timer_never_retries_finished_incidents_automatically(scheduler):
    universe, _ = scheduler.start(2)
    finished, _ = scheduler.step(universe, continue_review=True, max_batches=1)
    assert not finished.pending and finished.retryable == tuple(universe)
    assert scheduler.step(universe, continue_review=True, max_batches=1) == (finished, True)
    assert len(scheduler.batches) == 1


def test_fragment_first_render_defers_io_then_next_tick_processes_one_block(scheduler):
    universe, review = scheduler.start(13)
    scheduler.state.update(_radar_review_execution={
        "owner": scheduler.journal.owner, "run_id": review.run_id, "view": "radar",
    }, _radar_fragment_defer=True)
    fragment = inspect.unwrap(app._radar_review_fragment)
    kwargs = dict(strategy=StrategyConfig(), history_days=730, auto_adjust=True)
    fragment(scheduler.journal, universe, **kwargs)
    assert not scheduler.batches
    assert "_radar_fragment_defer" not in scheduler.state
    assert scheduler.statuses[-1][1]["activity"] == "Preparando primer bloque"
    fragment(scheduler.journal, universe, **kwargs)
    assert scheduler.batches == [tuple(universe[:BATCH_SIZE])]
    current = RadarReview.from_checkpoint(scheduler.journal.get_radar_review_checkpoint())
    assert len(current.outcomes) == BATCH_SIZE


def test_fragment_finished_queue_clears_execution_without_retrying(scheduler):
    universe, _ = scheduler.start(2)
    review, _ = scheduler.step(universe, continue_review=True, max_batches=1)
    scheduler.state["_radar_review_execution"] = {
        "owner": scheduler.journal.owner, "run_id": review.run_id, "view": "radar",
    }
    with pytest.raises(_FullRerun):
        inspect.unwrap(app._radar_review_fragment)(scheduler.journal, universe,
            strategy=StrategyConfig(), history_days=730, auto_adjust=True)
    assert "_radar_review_execution" not in scheduler.state
    assert len(scheduler.batches) == 1


def test_leaving_work_view_disarms_execution_and_return_does_not_resume(scheduler):
    universe, review = scheduler.start(13)
    execution = {"owner": scheduler.journal.owner, "run_id": review.run_id, "view": "radar"}
    scheduler.state.update(_radar_review_execution=execution, _radar_fragment_defer=True)
    app._bind_radar_execution_view("radar")
    assert scheduler.state["_radar_review_execution"] == execution
    app._bind_radar_execution_view("favoritas")
    assert "_radar_review_execution" not in scheduler.state
    assert "_radar_fragment_defer" not in scheduler.state
    app._bind_radar_execution_view("radar")
    assert scheduler.step(universe) == (review, True)
    assert not scheduler.batches
