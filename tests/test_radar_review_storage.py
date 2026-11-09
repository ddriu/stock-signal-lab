from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from src.journal import ANALYSIS_SNAPSHOT_COLUMNS, RADAR_CHECKPOINT_MAX_BYTES, TradingJournal
from src.supabase_journal import JournalStorageError, SupabaseTradingJournal


def test_local_checkpoint_survives_reopen_and_is_owner_scoped(tmp_path):
    path = tmp_path / "checkpoint.sqlite"
    alice = TradingJournal(path, owner="alice")
    bob = TradingJournal(path, owner="bob")
    assert alice.get_radar_review_checkpoint() is None
    assert not alice.save_radar_review_checkpoint({"progress": 9}, expected_revision=1)
    assert alice.save_radar_review_checkpoint({"progress": 5}, expected_revision=None)
    assert not alice.save_radar_review_checkpoint({"progress": 0}, expected_revision=None)
    assert bob.get_radar_review_checkpoint() is None
    assert bob.save_radar_review_checkpoint({"progress": 6}, expected_revision=None)
    reopened = TradingJournal(path, owner="alice")
    assert reopened.get_radar_review_checkpoint() == {"progress": 5, "revision": 1}
    assert reopened.save_radar_review_checkpoint(
        {"progress": 10, "revision": 999}, expected_revision=1
    )
    assert not alice.save_radar_review_checkpoint({"progress": 15}, expected_revision=1)
    assert alice.get_radar_review_checkpoint() == {"progress": 10, "revision": 2}
    assert bob.get_radar_review_checkpoint() == {"progress": 6, "revision": 1}


def test_only_one_local_writer_can_advance_a_revision(tmp_path):
    journal = TradingJournal(tmp_path / "checkpoint.sqlite", owner="alice")
    assert journal.save_radar_review_checkpoint({"progress": 0}, expected_revision=None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda progress: journal.save_radar_review_checkpoint(
                {"progress": progress}, expected_revision=1,
            ),
            (5, 10),
        ))
    assert sorted(results) == [False, True]
    assert journal.get_radar_review_checkpoint()["revision"] == 2


@pytest.mark.parametrize("payload", [
    {"owner": "bob"},
    {"score": float("inf")},
    {"symbols": ("NOT", "JSON")},
    {"nested": {1: "key"}},
    {"notes": "x" * RADAR_CHECKPOINT_MAX_BYTES},
])
def test_invalid_checkpoint_never_overwrites_valid_local_state(tmp_path, payload):
    journal = TradingJournal(tmp_path / "checkpoint.sqlite", owner="alice")
    assert journal.save_radar_review_checkpoint({"progress": 3}, expected_revision=None)
    with pytest.raises(ValueError):
        journal.save_radar_review_checkpoint(payload, expected_revision=1)
    assert journal.get_radar_review_checkpoint() == {"progress": 3, "revision": 1}


@pytest.mark.parametrize("revision", [False, 0, -1, 1.5, "1", 2_147_483_647])
def test_invalid_expected_revision_is_not_a_conflict(tmp_path, revision):
    journal = TradingJournal(tmp_path / "checkpoint.sqlite", owner="alice")
    with pytest.raises(ValueError):
        journal.save_radar_review_checkpoint({"progress": 1}, expected_revision=revision)


def test_supabase_uses_exact_owner_filter_and_cas_rpc(monkeypatch):
    journal = SupabaseTradingJournal("https://example.test", "sb_secret_synthetic", "alice")
    calls = []
    responses = iter([
        [{"owner": "alice", "revision": 1, "checkpoint": {"progress": 5}}],
        [{"owner": "alice", "revision": 2, "checkpoint": {"progress": 10}}],
        True, False,
    ])

    def request(method, **kwargs):
        calls.append((method, kwargs))
        return SimpleNamespace(json=lambda: next(responses))

    monkeypatch.setattr(journal, "_request", request)
    assert journal.get_radar_review_checkpoint() == {"progress": 5, "revision": 1}
    assert journal.get_radar_review_checkpoint() == {"progress": 10, "revision": 2}
    assert journal.save_radar_review_checkpoint({"progress": 5, "revision": 999}, expected_revision=None)
    assert not journal.save_radar_review_checkpoint({"progress": 5}, expected_revision=1)
    for method, kwargs in calls[:2]:
        assert method == "GET"
        assert kwargs["endpoint"].endswith("/rest/v1/radar_review_checkpoints")
        assert kwargs["params"]["owner"] == "eq.alice"
    for revision, (method, kwargs) in zip((None, 1), calls[2:], strict=True):
        assert method == "POST"
        assert kwargs["endpoint"].endswith("/rest/v1/rpc/save_radar_review_checkpoint")
        assert kwargs["json"] == {
            "p_owner": "alice", "p_expected_revision": revision, "p_checkpoint": {"progress": 5},
        }


@pytest.mark.parametrize("invalid_response", [
    {"owner": "alice", "revision": 1, "checkpoint": {}},
    [{"owner": "bob", "revision": 1, "checkpoint": {}}],
    [{"owner": "alice", "revision": True, "checkpoint": {}}],
    [{"owner": "alice", "revision": 1, "checkpoint": []}],
])
def test_supabase_rejects_ambiguous_or_cross_owner_read(monkeypatch, invalid_response):
    journal = SupabaseTradingJournal("https://example.test", "sb_secret_synthetic", "alice")
    monkeypatch.setattr(journal, "_request", lambda *args, **kwargs: SimpleNamespace(
        json=lambda: invalid_response,
    ))
    with pytest.raises(JournalStorageError):
        journal.get_radar_review_checkpoint()


def test_supabase_does_not_treat_ambiguous_write_as_cas_conflict(monkeypatch):
    journal = SupabaseTradingJournal("https://example.test", "sb_secret_synthetic", "alice")
    monkeypatch.setattr(journal, "_request", lambda *args, **kwargs: SimpleNamespace(
        json=lambda: 0,
    ))
    with pytest.raises(JournalStorageError):
        journal.save_radar_review_checkpoint({"progress": 3}, expected_revision=1)


def test_sqlite_batch_filter_applies_before_bounded_limit(tmp_path):
    journal = TradingJournal(tmp_path / "checkpoint.sqlite", owner="alice")

    def snapshot(ticker, session):
        return journal.add_analysis_snapshot(
            ticker=ticker, analyzed_at=session, price=100, opportunity_score=50,
            company_score=60, entry_score=50, valuation_score=50, relative_score=50,
            risk_score=50, opportunity_label="Esperar", entry_label="Esperar",
            position_label="Mantener",
        )

    current = snapshot("RDS-A", "2026-11-09")
    snapshot("RDS-A", "2026-11-06")
    distractor = snapshot("OTHER", "2026-11-09")
    columns = [column for column in ANALYSIS_SNAPSHOT_COLUMNS if column != "id"]
    with journal._connect() as connection:
        record = connection.execute(
            "SELECT * FROM analysis_snapshots WHERE id=?", (distractor,),
        ).fetchone()
        connection.executemany(
            f"INSERT INTO analysis_snapshots ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [tuple(record[column] for column in columns)] * 1001,
        )
    frame = journal.list_analysis_snapshots_for_sessions(["2026-11-09"], tickers=["rds-a", "RDS-A"])
    assert frame["id"].tolist() == [current]
    assert not frame.attrs["possibly_truncated"]
    assert journal.list_analysis_snapshots_for_sessions(["2026-11-09"]).attrs["possibly_truncated"]


def test_supabase_radar_batch_lookup_scopes_owner_day_and_ticker(monkeypatch):
    journal = SupabaseTradingJournal("https://example.test", "sb_secret_synthetic", "alice")
    captured = {}
    rows = [{"owner": "alice", "ticker": "BRK-B", "analyzed_at": "2026-11-09T00:00:00Z"}]

    def request(method, **kwargs):
        assert method == "GET"
        captured.update(kwargs)
        return SimpleNamespace(json=lambda: rows, headers={"Content-Range": "0-0/2"})

    monkeypatch.setattr(journal, "_request", request)
    result = journal.list_analysis_snapshots_for_sessions(
        ["2026-11-09"], tickers=["brk-b", "BRK-B", "0700.HK"],
    )
    assert captured["params"]["owner"] == "eq.alice"
    assert captured["params"]["ticker"] == 'in.("0700.HK","BRK-B")'
    assert captured["params"]["or"] == (
        "(and(analyzed_at.gte.2026-11-09T00:00:00Z,analyzed_at.lt.2026-11-10T00:00:00Z))"
    )
    assert captured["params"]["limit"] == "1000"
    assert captured["headers"]["Prefer"] == "count=exact"
    assert result["ticker"].tolist() == ["BRK-B"]
    assert result.attrs["possibly_truncated"]
    rows[0]["ticker"] = "OTHER"
    with pytest.raises(JournalStorageError, match="no solicitados"):
        journal.list_analysis_snapshots_for_sessions(["2026-11-09"], tickers=["BRK-B"])


@pytest.mark.parametrize("tickers", ["ABC", ["ABC,DEF"], ["X" * 65], [None], ["ABC"] * 26, []])
def test_invalid_or_empty_batch_filter_never_reads_the_database(monkeypatch, tmp_path, tickers):
    local = TradingJournal(tmp_path / "checkpoint.sqlite", owner="alice")
    remote = SupabaseTradingJournal("https://example.test", "sb_secret_synthetic", "alice")

    def fail(*args, **kwargs):
        pytest.fail("An empty or invalid batch filter must not read history")

    monkeypatch.setattr(local, "_connect", fail)
    monkeypatch.setattr(remote, "_request", fail)
    for journal in (local, remote):
        if tickers == []:
            assert journal.list_analysis_snapshots_for_sessions(["2026-11-09"], tickers=[]).empty
        else:
            with pytest.raises(ValueError):
                journal.list_analysis_snapshots_for_sessions(["2026-11-09"], tickers=tickers)
