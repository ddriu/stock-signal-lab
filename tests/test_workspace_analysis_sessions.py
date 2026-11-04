"""Bounded workspace context uses saved observations, not provider rebuilds."""
from collections import Counter
from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from src.journal import ANALYSIS_SNAPSHOT_COLUMNS, TradingJournal, normalize_analysis_sessions
from src.session_journal import session_journal
from src.supabase_journal import JournalStorageError, SupabaseTradingJournal


def add_observation(journal, day="2026-10-23", ticker="TEST"):
    return journal.add_analysis_snapshot(
        ticker=ticker, analyzed_at=day, price=100, opportunity_score=80,
        company_score=80, entry_score=80, valuation_score=70, relative_score=70,
        risk_score=60, opportunity_label="Candidata", entry_label="Esperar",
        position_label="Mantener", note="Contexto guardado con tildes: revisión.",
    )


def row(day="2026-10-23T22:00:00+00:00", owner="alice", row_id=1):
    return {"owner": owner, "id": row_id, "ticker": "TEST", "analyzed_at": day,
            "price": 100, "entry_score": 80, "note": "Contexto original"}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


@pytest.mark.parametrize("sessions", [
    "2026-10-23", None, {"2026-10-23"}, [date(2026, 10, 23)], [None],
    ["2026-1-3"], ["2026-10-23T00:00:00"], ["2026-02-30"], ["today"],
    ["2026-10-23') OR 1=1 --"], [f"2026-10-{day:02}" for day in range(1, 7)],
])
def test_sessions_reject_non_iso_or_more_than_five_before_read(sessions, monkeypatch, tmp_path):
    local = TradingJournal(tmp_path / "journal.sqlite", owner="alice")
    remote = SupabaseTradingJournal("https://example.supabase.co", "sb_secret_test", "alice")

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid session input must not reach a database")

    monkeypatch.setattr(local, "_connect", forbidden)
    monkeypatch.setattr(remote, "_request", forbidden)
    for journal in (local, remote):
        with pytest.raises(ValueError):
            journal.list_analysis_snapshots_for_sessions(sessions)


def test_empty_and_duplicate_sessions_are_normalized_without_database_reads(monkeypatch, tmp_path):
    assert normalize_analysis_sessions(["2026-10-22", "2026-10-23", "2026-10-22"]) == (
        "2026-10-23", "2026-10-22")
    journals = (TradingJournal(tmp_path / "journal.sqlite", owner="alice"),
                SupabaseTradingJournal("https://example.supabase.co", "sb_secret_test", "alice"))

    def forbidden(*args, **kwargs):
        pytest.fail("Empty sessions must not read any history")

    monkeypatch.setattr(journals[0], "_connect", forbidden)
    monkeypatch.setattr(journals[1], "_request", forbidden)
    for journal in journals:
        frame = journal.list_analysis_snapshots_for_sessions([])
        assert frame.empty and list(frame.columns) == ANALYSIS_SNAPSHOT_COLUMNS
        assert frame.attrs == {"requested_sessions": [], "bounded_limit": 1000,
                               "possibly_truncated": False}


def test_sqlite_exact_sessions_intraday_utc_boundaries_readonly_and_no_rebuild(monkeypatch, tmp_path):
    journal = TradingJournal(tmp_path / "journal.sqlite", owner="alice")
    first = add_observation(journal, "2026-10-23T00:00:00", "FIRST")
    last = add_observation(journal, "2026-10-23T23:59:59.999999Z", "LAST")
    utc = add_observation(journal, "2026-10-24T00:30:00+02:00", "UTC")
    add_observation(journal, "2026-10-22T23:59:59Z", "OLD")
    add_observation(journal, "2026-10-24T00:00:00Z", "FUTURE")
    add_observation(journal, "2026-10-21", "GAP")
    connect = journal._connect

    def readonly():
        connection = connect()
        connection.execute("PRAGMA query_only=ON")
        return connection

    monkeypatch.setattr(journal, "_connect", readonly)
    monkeypatch.setattr(journal, "list_analysis_snapshots", lambda *args: pytest.fail("Full history forbidden"))
    selected = journal.list_analysis_snapshots_for_sessions(["2026-10-23", "2026-10-23"])
    assert set(selected["id"]) == {first, last, utc}
    assert list(selected.columns) == ANALYSIS_SNAPSHOT_COLUMNS
    assert selected.attrs["possibly_truncated"] is False
    assert set(selected["ticker"]) == {"FIRST", "LAST", "UTC"}
    selected.loc[0, "note"] = "Changed copy"
    assert "Changed copy" not in journal.list_analysis_snapshots_for_sessions(["2026-10-23"])["note"].tolist()


def test_sqlite_result_cap_is_explicit_and_does_not_fill_gaps(tmp_path):
    journal = TradingJournal(tmp_path / "journal.sqlite", owner="alice")
    first = add_observation(journal)
    with journal._connect() as connection:
        original = dict(connection.execute("SELECT * FROM analysis_snapshots WHERE id=?", (first,)).fetchone())
        columns = [column for column in ANALYSIS_SNAPSHOT_COLUMNS if column != "id"]
        connection.executemany(
            f"INSERT INTO analysis_snapshots ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [tuple(original[column] for column in columns)] * 1001,
        )
    selected = journal.list_analysis_snapshots_for_sessions(["2026-10-23", "2026-10-21"])
    assert len(selected) == 1000
    assert selected.iloc[0]["id"] == 1002 and selected.iloc[-1]["id"] == 3
    assert selected.attrs["possibly_truncated"] is True
    assert journal.list_analysis_snapshots_for_sessions(["2026-10-21"]).empty


def test_supabase_context_is_exact_owner_filtered_bounded_query_without_pagination(monkeypatch):
    journal = SupabaseTradingJournal("https://example.supabase.co", "sb_secret_test", "alice")
    calls = []

    def request(method, **kwargs):
        calls.append((method, kwargs))
        return Response([row(), row("2026-10-22T00:00:00Z", row_id=2)])

    monkeypatch.setattr(journal, "_request", request)
    monkeypatch.setattr(journal, "_list_rows", lambda **kwargs: pytest.fail("Full history pagination forbidden"))
    selected = journal.list_analysis_snapshots_for_sessions(["2026-10-22", "2026-10-23"])
    assert len(calls) == 1 and calls[0][0] == "GET"
    params = calls[0][1]["params"]
    assert params["owner"] == "eq.alice" and params["limit"] == "1000"
    assert params["select"] == "owner," + ",".join(ANALYSIS_SNAPSHOT_COLUMNS)
    assert params["or"] == (
        "(and(analyzed_at.gte.2026-10-23T00:00:00Z,analyzed_at.lt.2026-10-24T00:00:00Z),"
        "and(analyzed_at.gte.2026-10-22T00:00:00Z,analyzed_at.lt.2026-10-23T00:00:00Z))")
    assert list(selected.columns) == ANALYSIS_SNAPSHOT_COLUMNS
    assert selected["confidence_pct"].isna().all()
    assert selected.attrs["requested_sessions"] == ["2026-10-23", "2026-10-22"]


def test_supabase_utc_boundary_and_legacy_confidence_fallback_keep_filters(monkeypatch):
    journal = SupabaseTradingJournal("https://example.supabase.co", "sb_secret_test", "alice")
    calls = []

    def request(method, **kwargs):
        calls.append(dict(kwargs["params"]))
        if len(calls) == 1:
            raise JournalStorageError("Legacy", status_code=400,
                                      response_body="column confidence_pct does not exist")
        return Response([row("2026-10-24T00:30:00+02:00")])

    monkeypatch.setattr(journal, "_request", request)
    selected = journal.list_analysis_snapshots_for_sessions(["2026-10-23"])
    assert len(selected) == 1 and len(calls) == 2
    assert calls[0]["owner"] == calls[1]["owner"] == "eq.alice"
    assert calls[0]["or"] == calls[1]["or"]
    assert "confidence_pct" in calls[0]["select"] and "confidence_pct" not in calls[1]["select"]


@pytest.mark.parametrize("payload", [
    None, {}, [None], ["invalid"], [row(owner="bob")], [{**row(), "owner": None}],
    [row("invalid")], [row("2026-10-24T00:00:00Z")], [row("2026-10-22T23:59:59Z")],
    [{**row(), "analyzed_at": None}], [row()] * 1001,
])
def test_supabase_context_rejects_cross_owner_malformed_or_unrequested_dates(monkeypatch, payload):
    journal = SupabaseTradingJournal("https://example.supabase.co", "sb_secret_test", "alice")
    monkeypatch.setattr(journal, "_request", lambda *args, **kwargs: Response(payload))
    with pytest.raises(JournalStorageError):
        journal.list_analysis_snapshots_for_sessions(["2026-10-23"])


class CachedJournal:
    def __init__(self, owner="alice"):
        self.owner, self.calls = owner, Counter()

    def list_analysis_snapshots_for_sessions(self, sessions):
        self.calls["read"] += 1
        result = pd.DataFrame([{"ticker": "TEST", "note": ["original"]}])
        result.attrs["requested_sessions"] = list(sessions)
        return result

    def add_analysis_snapshot(self):
        self.calls["write"] += 1


def test_context_session_cache_ttl_arguments_owner_and_save_invalidation():
    alice, bob, state = CachedJournal(), CachedJournal("bob"), {}
    a, b = session_journal(alice, state), session_journal(bob, state)
    with patch("src.session_journal.time.monotonic", return_value=100):
        original = a.list_analysis_snapshots_for_sessions(["2026-10-23"])
        original.loc[0, "note"].append("mutated")
        original.attrs["requested_sessions"].append("mutated")
        cached = a.list_analysis_snapshots_for_sessions(sessions=["2026-10-23"])
        assert cached.loc[0, "note"] == ["original"]
        assert cached.attrs["requested_sessions"] == ["2026-10-23"]
        b.list_analysis_snapshots_for_sessions(["2026-10-23"])
        assert alice.calls == bob.calls == {"read": 1}
    assert "_view_data_revision" not in state
    with patch("src.session_journal.time.monotonic", return_value=159.99):
        a.list_analysis_snapshots_for_sessions(["2026-10-23"])
    assert alice.calls == {"read": 1}
    with patch("src.session_journal.time.monotonic", return_value=160):
        a.list_analysis_snapshots_for_sessions(["2026-10-23"])
        a.list_analysis_snapshots_for_sessions(["2026-10-22"])
        assert alice.calls == {"read": 3}
        a.add_analysis_snapshot()
        a.list_analysis_snapshots_for_sessions(["2026-10-23"])
    assert alice.calls == {"read": 4, "write": 1}
    assert state["_view_data_revision"] == 1
