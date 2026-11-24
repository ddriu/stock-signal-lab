"""Offline Radar/report wiring; market, SMTP and Arena are never executed."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import app
from config import StrategyConfig
from src.journal import TradingJournal
from src.radar_review import BATCH_SIZE, RadarOutcome, RadarReview
from src.supabase_journal import JournalStorageError


class _Slot:
    def empty(self):
        pass

    def container(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    state, batches, archives = {}, [], []
    journal = TradingJournal(tmp_path / "radar-report.sqlite", owner="synthetic-owner")
    monkeypatch.setattr(app, "st", SimpleNamespace(
        session_state=state, empty=_Slot, info=lambda *_: None,
        warning=lambda *_: None, caption=lambda *_: None,
    ))
    monkeypatch.setattr(app, "render_automatic_review_status", lambda *_a, **_k: None)

    def batch(journal, review, worker, **kwargs):
        claimed = review.claim(worker, datetime.now(timezone.utc))
        app._save_radar_checkpoint(journal, claimed)
        tickers = claimed.pending[:BATCH_SIZE]
        batches.append(tickers)
        finished = claimed.release({ticker: RadarOutcome("no_data") for ticker in tickers})
        app._save_radar_checkpoint(journal, finished)
        return finished

    def archive(journal, **kwargs):
        archives.append((journal.owner, kwargs))
        return SimpleNamespace(status="blocked", session="2026-11-23",
            report_id="a" * 64, authorized=False, error_count=1)

    monkeypatch.setattr(app, "_run_radar_batch", batch)
    monkeypatch.setattr(app, "archive_saved_close_report", archive, raising=False)
    universe = [f"TEST{i:02}" for i in range(BATCH_SIZE + 1)]

    def step(**kwargs):
        return app._radar_review_step(journal, universe, strategy=StrategyConfig(),
            history_days=730, auto_adjust=True, **kwargs)

    state["_force_all_favorite_refresh"] = True
    review, _ = step(max_batches=0)
    return SimpleNamespace(state=state, batches=batches, archives=archives,
        journal=journal, universe=universe, review=review, step=step)


def test_last_explicit_batch_archives_report_without_repeating_market_work(bridge):
    assert bridge.archives == []
    review, compatible = bridge.step(continue_review=True, max_batches=1)
    assert compatible and review.pending and bridge.archives == []
    review, compatible = bridge.step(continue_review=True, max_batches=1)
    assert compatible and not review.pending
    assert len(bridge.archives) == 1
    assert bridge.archives[0][0] == bridge.journal.owner
    assert bridge.archives[0][1]["reference_time"].tzinfo is not None
    assert bridge.state["_radar_review_report_result"] == {
        "owner": bridge.journal.owner, "run_id": review.run_id,
        "status": "blocked", "session": "2026-11-23", "report_id": "a" * 64,
        "authorized": False, "error_count": 1,
    }

    for _ in range(3):
        bridge.step(allow_work=False)
        bridge.step()
    assert len(bridge.archives) == 1
    assert len(bridge.batches) == 2


def test_report_storage_failure_keeps_completed_review_and_has_explicit_retry(bridge, monkeypatch):
    def failed(*_args, **_kwargs):
        raise RuntimeError("PRIVATE_BACKEND_RESPONSE")

    monkeypatch.setattr(app, "archive_saved_close_report", failed, raising=False)
    review, compatible = bridge.step(continue_review=True)
    assert compatible and not review.pending
    assert RadarReview.from_checkpoint(bridge.journal.get_radar_review_checkpoint()) == review
    assert "_radar_review_notice" not in bridge.state
    notice = bridge.state["_radar_review_report_notice"]
    assert "informe" in notice.lower()
    assert "PRIVATE_BACKEND_RESPONSE" not in notice
    assert len(bridge.batches) == 2

    archives = []
    monkeypatch.setattr(app, "archive_saved_close_report", lambda *_a, **_k: (
        archives.append(True) or SimpleNamespace(status="verified", session="2026-11-23",
            report_id="b" * 64, authorized=True, error_count=0)))
    app._archive_completed_radar_report(bridge.journal, review)
    assert archives == [True]
    assert len(bridge.batches) == 2
    assert "_radar_review_report_notice" not in bridge.state
    assert bridge.state["_radar_review_report_result"]["authorized"] is True


def test_incomplete_review_cannot_archive_a_report_through_the_completion_hook(bridge):
    app._archive_completed_radar_report(bridge.journal, bridge.review)
    assert bridge.archives == []


def test_account_switch_clears_private_report_result(bridge):
    bridge.state.update(_navigation_view_owner="previous-owner",
        _radar_review_report_result={"owner": "previous-owner", "report_id": "a" * 64},
        _radar_review_report_notice="Previous private state")
    app._bind_navigation_owner(bridge.journal.owner)
    assert "_radar_review_report_result" not in bridge.state
    assert "_radar_review_report_notice" not in bridge.state


def test_unconfirmed_last_block_never_archives(bridge, monkeypatch):
    bridge.step(continue_review=True, max_batches=1)
    assert bridge.archives == []

    def conflicted(*_args, **_kwargs):
        raise JournalStorageError("PRIVATE_CAS_DETAIL")

    monkeypatch.setattr(app, "_run_radar_batch", conflicted)
    review, compatible = bridge.step(continue_review=True, max_batches=1)
    assert compatible and len(review.pending) == 1
    assert bridge.archives == []
    assert "_radar_review_report_result" not in bridge.state


def test_cancelled_last_block_never_archives(bridge, monkeypatch):
    class Cancelled(BaseException):
        pass

    bridge.step(continue_review=True, max_batches=1)

    def cancelled(*_args, **_kwargs):
        raise Cancelled

    monkeypatch.setattr(app, "_run_radar_batch", cancelled)
    with pytest.raises(Cancelled):
        bridge.step(continue_review=True, max_batches=1)
    assert bridge.archives == []
    saved = RadarReview.from_checkpoint(bridge.journal.get_radar_review_checkpoint())
    assert len(saved.pending) == 1
