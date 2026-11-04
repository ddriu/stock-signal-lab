"""Latest archived report reads use synthetic stores, never market providers."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from unittest.mock import patch

import pytest

from src.journal import TradingJournal
from src.session_journal import session_journal
from src.supabase_journal import JournalStorageError, SupabaseTradingJournal


def sealed_report(market_date="2026-10-23", score=82):
    report = {"as_of": market_date, "version": "original-test", "rows": [
        {"ticker": "TEST", "company_name": "Compañía de prueba", "score": score}]}
    report["report_id"] = hashlib.sha256(json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return report


def stored_row(report, owner="alice"):
    return {"owner": owner, "report_id": report["report_id"],
            "market_date": report["as_of"], "report_json": report}


def test_sqlite_latest_uses_market_date_before_insertion_time_and_owner(tmp_path):
    path = tmp_path / "journal.sqlite"
    alice, bob = TradingJournal(path, owner="alice"), TradingJournal(path, owner="bob")
    assert alice.get_latest_daily_score_report() is None
    latest, old = sealed_report(), sealed_report("2026-10-22")
    alice.save_daily_score_report(latest)
    alice.save_daily_score_report(old)
    bob.save_daily_score_report(sealed_report("2026-10-26"))
    assert alice.get_latest_daily_score_report() == latest
    assert alice.get_daily_score_report(old["report_id"]) == old
    assert bob.get_latest_daily_score_report()["as_of"] == "2026-10-26"
    assert TradingJournal(path, owner="other").get_latest_daily_score_report() is None


def test_sqlite_latest_breaks_ties_by_created_at_then_report_id(tmp_path):
    journal = TradingJournal(tmp_path / "journal.sqlite", owner="alice")
    first, second = sealed_report(score=82), sealed_report(score=83)
    journal.save_daily_score_report(first)
    journal.save_daily_score_report(second)
    with journal._connect() as connection:
        connection.execute("UPDATE daily_score_reports SET created_at=? WHERE report_id=?",
                           ("2026-10-24T23:00:00+00:00", first["report_id"]))
        connection.execute("UPDATE daily_score_reports SET created_at=? WHERE report_id=?",
                           ("2026-10-24T22:00:00+00:00", second["report_id"]))
    assert journal.get_latest_daily_score_report() == first
    with journal._connect() as connection:
        connection.execute("UPDATE daily_score_reports SET created_at=?",
                           ("2026-10-24T23:00:00+00:00",))
    assert journal.get_latest_daily_score_report() == max(
        (first, second), key=lambda report: report["report_id"])


def test_sqlite_latest_does_not_write_or_rebuild_and_returns_independent_json(tmp_path, monkeypatch):
    journal = TradingJournal(tmp_path / "journal.sqlite", owner="alice")
    original = sealed_report()
    journal.save_daily_score_report(original)
    connect = journal._connect

    def readonly_connect():
        connection = connect()
        connection.execute("PRAGMA query_only = ON")
        return connection

    def forbidden(*args, **kwargs):
        pytest.fail("Opening a saved report must not reconstruct analyses or operations")

    monkeypatch.setattr(journal, "_connect", readonly_connect)
    monkeypatch.setattr(journal, "list_analysis_snapshots", forbidden)
    monkeypatch.setattr(journal, "list_operations", forbidden)
    retrieved = journal.get_latest_daily_score_report()
    retrieved["rows"][0]["score"] = 0
    assert journal.get_latest_daily_score_report() == original
    assert journal.get_daily_score_report(original["report_id"]) == original


@pytest.mark.parametrize("change", ["json", "hash", "date", "id"])
def test_sqlite_latest_rejects_corruption_without_falling_back(tmp_path, change):
    journal = TradingJournal(tmp_path / "journal.sqlite", owner="alice")
    original = sealed_report()
    journal.save_daily_score_report(sealed_report("2026-10-22"))
    journal.save_daily_score_report(original)
    column, value = {
        "json": ("report_json", "not-json"),
        "hash": ("report_json", json.dumps({**original, "rows": []})),
        "date": ("market_date", "2026-10-24"),
        "id": ("report_id", "f" * 64),
    }[change]
    with journal._connect() as connection:
        connection.execute(f"UPDATE daily_score_reports SET {column}=? WHERE report_id=?",
                           (value, original["report_id"]))
    with pytest.raises(ValueError):
        journal.get_latest_daily_score_report()


class Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


@pytest.mark.parametrize("json_string", [False, True])
def test_supabase_latest_is_one_bounded_owner_filtered_read(monkeypatch, json_string):
    journal = SupabaseTradingJournal("https://example.supabase.co", "sb_secret_test", "alice")
    original = sealed_report()
    row = stored_row(original)
    if json_string:
        row["report_json"] = json.dumps(original)
    calls = []

    def request(method, **kwargs):
        calls.append((method, kwargs))
        return Response([row])

    monkeypatch.setattr(journal, "_request", request)
    assert journal.get_latest_daily_score_report() == original
    assert calls == [("GET", {
        "endpoint": "https://example.supabase.co/rest/v1/daily_score_reports",
        "params": {"owner": "eq.alice", "select": "owner,report_id,market_date,report_json",
                   "order": "market_date.desc,created_at.desc,report_id.desc", "limit": "1"},
    })]
    assert row["report_json"] == (json.dumps(original) if json_string else original)


def test_supabase_latest_empty_is_not_rebuilt(monkeypatch):
    journal = SupabaseTradingJournal("https://example.supabase.co", "sb_secret_test", "alice")
    calls = []

    def request(method, **kwargs):
        calls.append(method)
        return Response([])

    monkeypatch.setattr(journal, "_request", request)
    assert journal.get_latest_daily_score_report() is None
    assert calls == ["GET"]


@pytest.mark.parametrize("payload", [
    None, {}, [None], ["not a row"], [stored_row(sealed_report())] * 2,
    [stored_row(sealed_report(), owner="bob")],
    [{**stored_row(sealed_report()), "owner": None}],
    [{**stored_row(sealed_report()), "report_json": "invalid json"}],
    [{**stored_row(sealed_report()), "report_json": {}}],
    [{**stored_row(sealed_report()), "report_json": {**sealed_report(), "rows": []}}],
    [{**stored_row(sealed_report()), "report_id": "a" * 64}],
    [{**stored_row(sealed_report()), "market_date": "2026-10-22"}],
])
def test_supabase_latest_fails_closed_on_malformed_or_cross_owner_rows(monkeypatch, payload):
    journal = SupabaseTradingJournal("https://example.supabase.co", "sb_secret_test", "alice")
    monkeypatch.setattr(journal, "_request", lambda *args, **kwargs: Response(payload))
    with pytest.raises(JournalStorageError):
        journal.get_latest_daily_score_report()


class CachedJournal:
    def __init__(self, owner="alice"):
        self.owner, self.calls = owner, Counter()
        self.report = sealed_report()
        self.fail_read = False

    def get_latest_daily_score_report(self):
        self.calls["read"] += 1
        if self.fail_read:
            raise JournalStorageError("Synthetic read failure")
        return self.report

    def save_daily_score_report(self, report):
        self.calls["write"] += 1
        self.report = deepcopy(report)
        return report["report_id"]

    def list_operations(self):
        pytest.fail("Latest report must not load operations")


def test_latest_session_read_has_ttl_is_copied_and_save_invalidates():
    journal, state = CachedJournal(), {}
    proxy = session_journal(journal, state, ttl_seconds=60)
    with patch("src.session_journal.time.monotonic", return_value=100):
        proxy.get_latest_daily_score_report()["rows"][0]["score"] = 0
    with patch("src.session_journal.time.monotonic", return_value=159.99):
        assert proxy.get_latest_daily_score_report() == journal.report
        assert session_journal(journal, state).get_latest_daily_score_report() == journal.report
    assert journal.calls == {"read": 1}
    assert "_view_data_revision" not in state
    with patch("src.session_journal.time.monotonic", return_value=160):
        proxy.get_latest_daily_score_report()
    assert journal.calls == {"read": 2}
    next_report = sealed_report("2026-10-26")
    proxy.save_daily_score_report(next_report)
    assert proxy.get_latest_daily_score_report() == next_report
    assert journal.calls == {"read": 3, "write": 1}
    assert state["_view_data_revision"] == 1


def test_latest_session_read_is_owner_and_backend_scoped_and_rejects_owner_drift():
    alice, bob, state = CachedJournal(), CachedJournal("bob"), {}
    bob.report = sealed_report(score=75)
    first, second = session_journal(alice, state), session_journal(bob, state)
    assert first.get_latest_daily_score_report() == alice.report
    assert second.get_latest_daily_score_report() == bob.report
    assert first.get_latest_daily_score_report() == alice.report
    assert alice.calls == bob.calls == {"read": 1}
    other_backend = CachedJournal()
    session_journal(other_backend, state).get_latest_daily_score_report()
    assert other_backend.calls == {"read": 1}
    cached_read = first.get_latest_daily_score_report
    alice.owner = "bob"
    with pytest.raises(RuntimeError, match="owner changed"):
        cached_read()
    assert alice.calls == {"read": 1}
    assert second.get_latest_daily_score_report() == bob.report
    assert bob.calls == {"read": 1}


def test_latest_failed_read_is_not_cached_or_treated_as_write():
    journal, state = CachedJournal(), {}
    proxy = session_journal(journal, state)
    journal.fail_read = True
    with pytest.raises(JournalStorageError):
        proxy.get_latest_daily_score_report()
    journal.fail_read = False
    assert proxy.get_latest_daily_score_report() == journal.report
    assert journal.calls == {"read": 2}
    assert "_view_data_revision" not in state
