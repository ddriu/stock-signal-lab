"""Local state/phase regressions; browser stale-DOM checks are separate."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd
from streamlit.testing.v1 import AppTest

import app
from config import StrategyConfig
from src.journal import TradingJournal
from src.radar_review import RadarReview


def test_navigation_peek_never_consumes_explicit_review_actions(tmp_path, monkeypatch):
    state = {"_force_all_favorite_refresh": True, "_radar_review_resume": True}
    monkeypatch.setattr(app, "st", SimpleNamespace(session_state=state))
    journal = TradingJournal(tmp_path / "peek.sqlite", owner="qa")
    assert app._radar_review_step(journal, ["TEST"], strategy=StrategyConfig(),
        history_days=730, auto_adjust=True, allow_work=False) == (None, False)
    assert state == {"_force_all_favorite_refresh": True, "_radar_review_resume": True}
    assert journal.get_radar_review_checkpoint() is None


def test_visible_phase_updates_replace_the_same_progress_surface():
    page = AppTest.from_string('''
from datetime import datetime, timezone
import streamlit as st
import app
from src.radar_review import RadarReview
now = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
review = RadarReview.start(["AAPL", "MSFT"], "a" * 64, now)
with app._analysis_result_surface(True):
    slot = st.empty()
    for phase in ("Consultando precio: AAPL", "Consultando fundamentales: AAPL"):
        slot.empty()
        with slot.container():
            app.render_automatic_review_status(review, compatible=True, now=now,
                working=True, activity=phase)
''', default_timeout=20).run()
    assert not page.exception
    bars = list(page.get("progress"))
    assert len(bars) == 1
    assert "Consultando fundamentales: AAPL" in bars[0].proto.text
    assert "Consultando precio" not in bars[0].proto.text


def test_phase_boundary_renews_the_existing_claim_without_changing_outcomes(tmp_path, monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    journal = TradingJournal(tmp_path / "heartbeat.sqlite", owner="qa")
    review = RadarReview.start(["TEST"], "a" * 64, Clock.now(timezone.utc))
    app._save_radar_checkpoint(journal, review)
    state, stages = {}, []
    monkeypatch.setattr(app, "st", SimpleNamespace(session_state=state))
    monkeypatch.setattr(app, "datetime", Clock)
    worker = uuid4().hex

    def load(tickers, *_args, **kwargs):
        initial = RadarReview.from_checkpoint(journal.get_radar_review_checkpoint())
        Clock.current += timedelta(seconds=181)
        kwargs["progress_callback"]("TEST", "Consultando fundamentales")
        renewed = RadarReview.from_checkpoint(journal.get_radar_review_checkpoint())
        assert renewed.worker == worker
        assert renewed.lease_until > initial.lease_until
        assert renewed.outcomes == initial.outcomes == {}
        return set()

    monkeypatch.setattr(app, "load_market_data", load)
    monkeypatch.setattr(app, "_analysis_view_context", lambda *a, **k: ({}, [], {}, {}, {}, {}, {}))
    final = app._run_radar_batch(journal, review, worker, history_days=730,
        auto_adjust=True, strategy=StrategyConfig(),
        progress_callback=lambda symbol, stage: stages.append((symbol, stage)))
    assert ("TEST", "Consultando fundamentales") in stages
    assert final.outcomes["TEST"].status == "no_data"
    assert final.worker is None


def test_prior_notice_is_not_shown_as_the_result_of_a_new_successful_review(tmp_path, monkeypatch):
    # The last phase is public diagnostic metadata, never a provider body/token.
    script = '''
import app
from config import StrategyConfig
from src.journal import TradingJournal
import streamlit as st
st.session_state["_force_all_favorite_refresh"] = True
def offline(*args, **kwargs):
    kwargs["progress_callback"]("SYNTH", "Consultando fundamentales")
    raise RuntimeError("PRIVATE_PROVIDER_BODY")
app.load_market_data = offline
journal = TradingJournal(":memory:", owner="qa")
app._radar_review_step(journal, ["SYNTH"], strategy=StrategyConfig(),
                       history_days=730, auto_adjust=True)
'''
    # SQLite in-memory connections are separate; use a real isolated tempfile.
    script = script.replace('":memory:"', repr(str(tmp_path / "notice.sqlite")))
    page = AppTest.from_string(script, default_timeout=20).run()
    assert not page.exception
    text = " ".join(w.value for w in page.warning)
    assert "Consultando fundamentales: SYNTH" in text
    assert "PRIVATE_PROVIDER_BODY" not in text
