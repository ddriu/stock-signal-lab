"""Owner-scoped report recovery with synthetic saved observations, no network."""
from copy import deepcopy
from datetime import datetime, timezone

import pandas as pd
import pytest

from src.analysis_policy import encode_policy_note
from src.saved_close_report import (
    archive_saved_close_report, build_saved_close_report, latest_completed_us_session,
)
from src.supabase_journal import JournalStorageError


def observation(ticker="SYNTH", day="2026-11-23", *, score=85, created=None):
    policy = {"policy_version": "dual_strategy_v1", "snapshot_method": "dual",
        "ticker": ticker, "as_of": day, "source": "Synthetic fixture", "price": 100,
        "currency": "USD", "config": {},
        "primary": {"id": "growth_momentum", "strategy_id": "growth_momentum",
            "score": score, "growth_score": 80, "momentum_score": 85,
            "context_score": 90, "status": "Entrada fuerte", "data_status": "complete"},
        "comparative": {}, "position": {"invalidated": False},
        "data_quality": {"status": "ok", "block_entry": False, "reasons": []}}
    return {"id": 1, "ticker": ticker, "analyzed_at": day,
        "created_at": created or day + "T22:00:00Z", "price": 100,
        "note": encode_policy_note("Synthetic", policy)}


class Journal:
    def __init__(self, *, owner="synthetic-a", favorites=("SYNTH",), observations=None):
        self.owner = owner
        self.favorites = pd.DataFrame({"ticker": list(favorites), "name": list(favorites)})
        self.observations = pd.DataFrame(observations if observations is not None else [observation()])
        self.queries, self.archived = [], {}
        self.truncated = False

    def list_favorites(self):
        return self.favorites.copy()

    def open_positions(self):
        return pd.DataFrame()

    def list_portfolio_snapshot_positions(self):
        return pd.DataFrame()

    def list_analysis_snapshots_for_sessions(self, sessions, *, tickers):
        self.queries.append((tuple(sessions), tuple(tickers)))
        if self.observations.empty:
            frame = self.observations.copy()
        else:
            frame = self.observations.loc[self.observations.ticker.isin(tickers)
                & self.observations.analyzed_at.isin(sessions)].copy()
        frame.attrs["possibly_truncated"] = self.truncated
        return frame

    def get_daily_score_report(self, report_id):
        return deepcopy(self.archived.get(report_id))

    def save_daily_score_report(self, report):
        self.archived.setdefault(report["report_id"], deepcopy(report))
        return report["report_id"]

    def __getattr__(self, name):
        if name.startswith(("claim_", "finish_", "send_", "save_", "list_enabled_")):
            pytest.fail(f"Archive must not access deliveries, other owners or portfolio writes: {name}")
        raise AttributeError(name)


def test_saved_archive_uses_current_owner_policy_and_readmits_recovered_favorite():
    from src.arena_universe_policy import build_arena_universe_policy
    from src.arena_data_quality import arena_report_authorized

    clock = "2026-11-24T16:15:00Z"
    journal = Journal(favorites=("SYNTH", "MISSING"))
    policy = build_arena_universe_policy(journal.owner, "verified_only",
        approved_at="2026-11-24T15:00:00Z", reference_time=clock)
    journal.get_arena_universe_policy = lambda: policy
    favorites_before = journal.favorites.copy(deep=True)
    first = archive_saved_close_report(journal, reference_time=clock)
    initial = deepcopy(journal.archived[first.report_id])
    assert first.authorized is True and initial["arena_universe"]["pending_count"] == 1
    assert initial["arena_universe"]["policy"] == policy
    assert arena_report_authorized(initial, owner=journal.owner) is True

    journal.observations = pd.DataFrame([observation(), observation("MISSING")])
    second = archive_saved_close_report(journal, reference_time=clock)
    recovered = journal.archived[second.report_id]
    assert second.authorized is True and recovered["arena_universe"]["pending_count"] == 0
    assert recovered["arena_quality"]["eligible_tickers"] == ["MISSING", "SYNTH"]
    assert recovered["arena_quality"]["explicit_exclusions"] == []
    assert journal.archived[first.report_id] == initial
    pd.testing.assert_frame_equal(journal.favorites, favorites_before)


def test_saved_archive_never_converts_failed_policy_read_into_a_strict_report():
    journal = Journal()
    def fail():
        raise RuntimeError("policy storage unavailable")
    journal.get_arena_universe_policy = fail
    with pytest.raises(RuntimeError, match="storage unavailable"):
        archive_saved_close_report(journal, reference_time="2026-11-24T16:15:00Z")
    assert journal.archived == {}


@pytest.mark.parametrize(("clock", "expected"), [
    # The two actual delayed starts on 24 November must recover the 23rd.
    ("2026-11-24T05:44:22Z", "2026-11-23"),
    ("2026-11-24T10:04:04Z", "2026-11-23"),
    ("2026-11-24T16:00:00Z", "2026-11-23"),
    (datetime(2026, 11, 24, 16, tzinfo=timezone.utc), "2026-11-23"),
    ("2026-11-24T21:00:00Z", "2026-11-24"),
    ("2026-11-26T17:00:00Z", "2026-11-25"),  # Thanksgiving.
    ("2026-11-27T17:59:59Z", "2026-11-25"),
    ("2026-11-27T18:00:00Z", "2026-11-27"),  # Early close.
    ("2026-11-29T12:00:00Z", "2026-11-27"),
])
def test_target_is_latest_closed_exchange_session_not_scheduler_or_calendar_day(clock, expected):
    assert latest_completed_us_session(reference_time=clock) == expected


def test_open_us_session_is_never_promoted_into_a_closed_report():
    data = pd.DataFrame([observation(), observation(day="2026-11-24", score=99,
        created="2026-11-24T16:00:00Z")])
    report = build_saved_close_report(data, favorite_tickers=["SYNTH"],
        reference_time="2026-11-24T16:15:00Z")
    assert report["as_of"] == "2026-11-23"
    assert report["rows"][0]["score"] == 85
    assert report["arena_quality"]["expected_session"] == "2026-11-23"
    assert report["arena_quality"]["authorized"] is True


def test_archive_updates_report_from_saved_inputs_without_deliveries_and_is_idempotent():
    journal = Journal()
    first = archive_saved_close_report(journal, reference_time="2026-11-24T10:04:04Z")
    second = archive_saved_close_report(journal, reference_time="2026-11-24T16:15:00Z")
    assert first.status == "verified" and first.authorized is True
    assert first.session == "2026-11-23" and len(first.report_id) == 64
    assert first.eligible_count == first.valid_count == 1 and first.error_count == 0
    assert second.report_id == first.report_id and second.unchanged is True
    assert len(journal.archived) == 1
    assert journal.archived[first.report_id]["review_mode"] == "saved_close_archive"
    assert journal.archived[first.report_id]["saved_close_report"]["mail_attempted"] is False
    sessions, tickers = journal.queries[0]
    assert len(sessions) == 5 and max(sessions) == "2026-11-23"
    assert tickers == ("SYNTH",)


def test_missing_favorite_is_archived_as_blocked_not_silently_removed():
    journal = Journal(favorites=("SYNTH", "MISSING"))
    result = archive_saved_close_report(journal, reference_time="2026-11-24T10:04:04Z")
    report = journal.archived[result.report_id]
    assert result.status == "blocked" and result.authorized is False
    assert result.error_count > 0
    assert report["arena_quality"]["favorite_tickers"] == ["MISSING", "SYNTH"]
    rows = {row["ticker"]: row for row in report["rows"]}
    assert rows["MISSING"]["score"] is None
    assert report["saved_close_report"]["status"] == "blocked"


def test_latest_closed_day_is_not_replaced_with_yesterdays_complete_scores():
    journal = Journal()
    result = archive_saved_close_report(journal, reference_time="2026-11-24T22:30:00Z")
    report = journal.archived[result.report_id]
    assert result.session == report["as_of"] == "2026-11-24"
    assert result.authorized is False and result.status == "blocked"
    assert report["rows"][0]["score"] is None


def test_large_universe_is_read_in_bounded_batches_without_truncating_favorites():
    favorites = tuple(f"T{i:03}" for i in range(175))
    journal = Journal(favorites=favorites, observations=[])
    result = archive_saved_close_report(journal, reference_time="2026-11-24T10:04:04Z")
    assert len(journal.queries) == 7
    assert all(len(sessions) == 5 and len(tickers) <= 25 for sessions, tickers in journal.queries)
    assert sorted(t for _, tickers in journal.queries for t in tickers) == sorted(favorites)
    assert len(journal.archived[result.report_id]["rows"]) == 175
    assert result.authorized is False


def test_two_accounts_never_share_favorites_observations_or_archives():
    first = Journal(owner="synthetic-a")
    second = Journal(owner="synthetic-b", favorites=("SECOND",), observations=[observation("SECOND")])
    for journal in (first, second):
        result = archive_saved_close_report(journal, reference_time="2026-11-24T10:04:04Z")
        assert journal.archived[result.report_id]["arena_quality"]["favorite_tickers"] == list(journal.favorites.ticker)
    assert set(first.archived).isdisjoint(second.archived)


@pytest.mark.parametrize("owner", ["", "anon", "anonymous", "public", "grupo_compartido"])
def test_unknown_or_shared_owner_is_rejected_without_reading_or_writing(owner):
    journal = Journal(owner=owner)
    with pytest.raises(ValueError):
        archive_saved_close_report(journal, reference_time="2026-11-24T10:04:04Z")
    assert not journal.queries and not journal.archived


def test_truncated_storage_or_unzoned_clock_never_archives_a_verified_report():
    journal = Journal()
    journal.truncated = True
    with pytest.raises(JournalStorageError):
        archive_saved_close_report(journal, reference_time="2026-11-24T10:04:04Z")
    assert not journal.archived
    with pytest.raises(ValueError):
        archive_saved_close_report(Journal(), reference_time="2026-11-24T10:04:04")


def test_empty_scope_preserves_existing_archive():
    journal = Journal(favorites=(), observations=[])
    result = archive_saved_close_report(journal, reference_time="2026-11-24T10:04:04Z")
    assert result.status == "empty" and not result.authorized and result.report_id == ""
    assert not journal.queries and not journal.archived


@pytest.mark.parametrize("ticker", ["UNKNOWN.ZZ", "EU.MC", "IE00B4L5Y983"])
def test_foreign_or_unknown_only_scope_never_authorizes_arena(ticker):
    journal = Journal(favorites=(ticker,), observations=[observation(ticker)])
    result = archive_saved_close_report(journal, reference_time="2026-11-24T16:00:00Z")
    report = journal.archived[result.report_id]
    assert result.session == report["as_of"] == "2026-11-23"
    assert result.status == "blocked" and result.authorized is False
    assert report["arena_quality"]["eligible_tickers"] == []
    assert report["saved_close_report"]["target_calendar"] == "XNYS"


def test_foreign_session_on_us_holiday_is_preserved_in_bounded_history():
    journal = Journal(favorites=("SYNTH", "IBE.MC"), observations=[
        observation("SYNTH", "2026-11-27"), observation("IBE.MC", "2026-11-27"),
        observation("IBE.MC", "2026-11-26", score=77),
    ])
    result = archive_saved_close_report(journal, reference_time="2026-11-27T23:00:00Z")
    report = journal.archived[result.report_id]
    rows = {row["ticker"]: row for row in report["rows"]}
    assert rows["IBE.MC"]["history"][1]["session"] == "2026-11-26"
    assert rows["IBE.MC"]["history"][1]["score"] == 77
    assert "2026-11-26" not in [item["session"] for item in rows["SYNTH"]["history"]]
    assert all(len(sessions) <= 5 and len(tickers) <= 25 for sessions, tickers in journal.queries)
    assert len(journal.queries) == 2


@pytest.mark.parametrize("ticker", [None, 2, ""])
def test_malformed_favorite_is_not_silently_excluded_from_coverage(ticker):
    journal = Journal()
    journal.favorites.loc[0, "ticker"] = ticker
    with pytest.raises(JournalStorageError):
        archive_saved_close_report(journal, reference_time="2026-11-24T16:00:00Z")
    assert not journal.archived


@pytest.mark.parametrize("owner", ["synthetic-b", pd.NA])
def test_explicit_wrong_or_nullable_owner_column_is_rejected(owner):
    journal = Journal()
    journal.favorites["owner"] = pd.Series([owner], dtype="string")
    with pytest.raises(JournalStorageError):
        archive_saved_close_report(journal, reference_time="2026-11-24T16:00:00Z")
    assert not journal.queries and not journal.archived


def test_held_only_row_does_not_become_an_arena_favorite():
    journal = Journal()
    journal.open_positions = lambda: pd.DataFrame({"ticker": ["HELD"]})
    result = archive_saved_close_report(journal, reference_time="2026-11-24T16:00:00Z")
    report = journal.archived[result.report_id]
    assert report["arena_quality"]["favorite_tickers"] == ["SYNTH"]
    assert report["arena_quality"]["eligible_tickers"] == ["SYNTH"]
    assert {row["ticker"] for row in report["rows"] if row["held"]} == {"HELD"}


def test_real_local_archive_preserves_prior_report_and_never_claims_a_delivery(tmp_path, monkeypatch):
    import src.journal as journal_module
    from src.daily_digest import seal_daily_score_report
    from src.journal import TradingJournal

    journal = TradingJournal(tmp_path / "archive.sqlite", owner="synthetic-private")
    journal.add_favorite("SYNTH", "Synthetic", "NASDAQ")

    class RecordedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 11, 23, 22, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    with monkeypatch.context() as patch:
        patch.setattr(journal_module, "datetime", RecordedClock)
        journal.add_analysis_snapshot(ticker="SYNTH", analyzed_at="2026-11-23", price=100,
            opportunity_score=85, company_score=80, entry_score=85, valuation_score=None,
            relative_score=None, risk_score=None, opportunity_label="Synthetic",
            entry_label="Entrada fuerte", position_label="Sin posición", note=observation()["note"])
    old = seal_daily_score_report({"as_of": "2026-11-20", "rows": []})
    journal.save_daily_score_report(old)
    before_favorites = journal.list_favorites().copy(deep=True)
    before_observations = journal.list_analysis_snapshots().copy(deep=True)
    first = archive_saved_close_report(journal, reference_time="2026-11-24T10:04:04Z")
    second = archive_saved_close_report(journal, reference_time="2026-11-24T16:00:00Z")
    assert first.authorized is True and second.unchanged is True
    assert journal.get_latest_daily_score_report()["report_id"] == first.report_id
    assert journal.get_daily_score_report(old["report_id"]) == old
    pd.testing.assert_frame_equal(journal.list_favorites(), before_favorites)
    pd.testing.assert_frame_equal(journal.list_analysis_snapshots(), before_observations)
    with journal._connect() as connection:
        assert connection.execute("select count(*) from daily_score_reports").fetchone()[0] == 2
        assert connection.execute("select count(*) from daily_digest_deliveries").fetchone()[0] == 0
        assert connection.execute("select count(*) from operations").fetchone()[0] == 0
