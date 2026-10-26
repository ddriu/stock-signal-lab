from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import pytest
from src.daily_digest_storage import normalized_digest_report
from src.journal import TradingJournal
from src.supabase_journal import JournalStorageError, SupabaseTradingJournal


def report():
    data = {"as_of": "2026-10-23", "version": "test", "rows": [
        {"ticker": "TEST", "company_name": "Compañía de prueba", "score": 82}]}
    data["report_id"] = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    return data


def test_sqlite_reports_are_immutable_and_scoped(tmp_path):
    journal = TradingJournal(tmp_path / "store.sqlite", owner="a")
    original = report()
    identifier = journal.save_daily_score_report(original)
    assert journal.save_daily_score_report(original) == identifier
    assert journal.get_daily_score_report(identifier) == original
    other = TradingJournal(tmp_path / "store.sqlite", owner="b")
    assert other.get_daily_score_report(identifier) is None
    with pytest.raises(ValueError, match="contenido"):
        journal.save_daily_score_report({**original, "rows": []})
    assert journal.get_daily_score_report(identifier) == original


def test_sqlite_reservation_is_atomic_and_never_recycled(tmp_path):
    journal = TradingJournal(tmp_path / "store.sqlite", owner="a")
    identifier = journal.save_daily_score_report(report())
    key = "a" * 64
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: journal.claim_daily_digest(key, "2026-10-23", identifier), range(12)))
    assert sum(claims) == 1
    journal.finish_daily_digest(key, "uncertain", "TimeoutError")
    assert not journal.claim_daily_digest(key, "2026-10-23", identifier)
    journal.finish_daily_digest(key, "sent")
    with journal._connect() as connection:
        saved = connection.execute("SELECT status,error_type FROM daily_digest_deliveries").fetchone()
        assert tuple(saved) == ("uncertain", "TimeoutError")
    other = TradingJournal(tmp_path / "store.sqlite", owner="b")
    other.finish_daily_digest(key, "failed")
    assert other.get_daily_score_report(identifier) is None


def test_claim_requires_owned_report_and_matching_session_and_no_secrets(tmp_path):
    journal = TradingJournal(tmp_path / "store.sqlite", owner="a")
    with pytest.raises(sqlite3.IntegrityError):
        journal.claim_daily_digest("a" * 64, "2026-10-23", "b" * 64)
    identifier = journal.save_daily_score_report(report())
    with pytest.raises(sqlite3.IntegrityError):
        journal.claim_daily_digest("a" * 64, "2026-10-22", identifier)
    with pytest.raises(ValueError):
        journal.finish_daily_digest("a" * 64, "failed", "password=secret")
    with pytest.raises(ValueError):
        journal.get_daily_score_report("bad-id")


class Response:
    def __init__(self, value):
        self.value = value
    def json(self):
        return self.value


def test_supabase_atomic_claim_filters_owner_and_requires_confirmation(monkeypatch):
    journal = SupabaseTradingJournal("https://example.supabase.co", "sb_secret_test", "a")
    calls = []
    responses = [Response([{"owner": "a", "delivery_key": "a" * 64}]), Response([]), Response({})]
    def request(method, **kwargs):
        calls.append((method, kwargs))
        return responses.pop(0)
    monkeypatch.setattr(journal, "_request", request)
    assert journal.claim_daily_digest("a" * 64, "2026-10-23", "b" * 64)
    assert not journal.claim_daily_digest("a" * 64, "2026-10-23", "b" * 64)
    journal.finish_daily_digest("a" * 64, "sent")
    assert calls[0][1]["json"]["owner"] == "a"
    assert calls[0][1]["params"]["on_conflict"] == "owner,delivery_key"
    assert "ignore-duplicates" in calls[0][1]["headers"]["Prefer"]
    assert calls[2][1]["params"] == {"owner": "eq.a", "delivery_key": "eq." + "a" * 64, "status": "eq.reserved"}
    monkeypatch.setattr(journal, "_request", lambda *a, **k: Response(None))
    with pytest.raises(JournalStorageError, match="reserva"):
        journal.claim_daily_digest("a" * 64, "2026-10-23", "b" * 64)


def test_supabase_immutable_report_lookup_never_reads_other_owner(monkeypatch):
    journal = SupabaseTradingJournal("https://example.supabase.co", "sb_secret_test", "a")
    original = report()
    calls = []
    def request(method, **kwargs):
        calls.append((method, kwargs))
        return Response([] if method == "POST" else [{"report_json": original}])
    monkeypatch.setattr(journal, "_request", request)
    assert journal.save_daily_score_report(original) == original["report_id"]
    assert calls[0][1]["json"]["owner"] == "a"
    assert calls[1][1]["params"]["owner"] == "eq.a"
    monkeypatch.setattr(journal, "_request", lambda *a, **k: Response([]))
    assert journal.get_daily_score_report(original["report_id"]) is None


def test_report_nan_or_wrong_date_or_wrong_identity_rejected():
    for updated in ({"rows": [{"score": float("nan")}]}, {"as_of": "today"}, {"report_id": "x"}):
        with pytest.raises(ValueError):
            normalized_digest_report({**report(), **updated})
