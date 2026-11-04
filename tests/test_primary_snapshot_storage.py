"""Synthetic local migrations: optional comparison is absence, never zero."""
from contextlib import contextmanager
import sqlite3

import pandas as pd
import pytest

from src.journal import TradingJournal, normalize_analysis_snapshot


def snapshot(**changes):
    return dict(ticker="SYNTH", analyzed_at="2026-11-03", price=100,
                opportunity_score=None, company_score=None, entry_score=85,
                valuation_score=None, relative_score=None, risk_score=None,
                opportunity_label="", entry_label="Entrada fuerte", position_label="Mantener",
                note="A dated primary policy may be present without a comparison.", **changes)


@contextmanager
def connect(path):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection


def legacy_database(path):
    journal = TradingJournal(path, owner="synthetic")
    with connect(path) as connection:
        original = connection.execute("SELECT sql FROM sqlite_schema WHERE name='analysis_snapshots'").fetchone()[0]
        legacy = original.replace("opportunity_score INTEGER\n", "opportunity_score INTEGER NOT NULL\n")
        assert legacy != original
        connection.execute("DROP TABLE analysis_snapshots")
        connection.execute(legacy)
        connection.execute("ALTER TABLE analysis_snapshots ADD COLUMN custom_text TEXT DEFAULT 'literal NOT NULL, with punctuation' NOT NULL")
        connection.execute("ALTER TABLE analysis_snapshots ADD COLUMN doubled_score INTEGER GENERATED ALWAYS AS (entry_score * 2) VIRTUAL")
        connection.execute("CREATE TABLE snapshot_parent(id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO snapshot_parent VALUES (1)")
        connection.execute("ALTER TABLE analysis_snapshots ADD COLUMN parent_key INTEGER REFERENCES snapshot_parent(id)")
        for identity in (7, 23, 800):
            connection.execute("""INSERT INTO analysis_snapshots
                (id,ticker,analyzed_at,price,opportunity_score,entry_score,note,created_at,parent_key)
                VALUES (?,?,?,?,?,?,?,?,?)""", (identity, f"SYNTH{identity}", "2026-11-03", 100,
                    76, 85, "Unicode policy preserved: ñ á — and JSON {}", "2026-11-04T01:00:00Z", 1))
        connection.execute("DELETE FROM analysis_snapshots WHERE id=800")
        connection.execute("CREATE TABLE snapshot_child(snapshot_id INTEGER REFERENCES analysis_snapshots(id) ON DELETE CASCADE)")
        connection.execute("INSERT INTO snapshot_child VALUES (7)")
        connection.execute("CREATE INDEX snapshot_custom_partial ON analysis_snapshots(custom_text) WHERE opportunity_score >= 50")
        connection.execute("CREATE TABLE snapshot_insert_log(snapshot_id INTEGER)")
        connection.execute("""CREATE TRIGGER snapshot_insert_audit AFTER INSERT ON analysis_snapshots
            BEGIN INSERT INTO snapshot_insert_log VALUES (new.id); END""")
        connection.execute("CREATE VIEW snapshot_custom_view AS SELECT id,custom_text,doubled_score FROM analysis_snapshots")
    return journal


def preserved_state(path):
    with connect(path) as connection:
        return dict(
            rows=[tuple(row) for row in connection.execute("SELECT * FROM analysis_snapshots ORDER BY id")],
            objects=[tuple(row) for row in connection.execute("SELECT name,sql FROM sqlite_schema WHERE name IN ('snapshot_custom_partial','snapshot_insert_audit','snapshot_custom_view') ORDER BY name")],
            child=[tuple(row) for row in connection.execute("SELECT * FROM snapshot_child")],
            sequence=connection.execute("SELECT seq FROM sqlite_sequence WHERE name='analysis_snapshots'").fetchone()[0],
            violations=[tuple(row) for row in connection.execute("PRAGMA foreign_key_check")],
        )


def test_new_storage_and_normalizer_keep_comparative_absence_not_zero(tmp_path):
    journal = TradingJournal(tmp_path / "new.sqlite", owner="synthetic")
    assert normalize_analysis_snapshot(**snapshot())["opportunity_score"] is None
    identity = journal.add_analysis_snapshot(**snapshot())
    record = journal.list_analysis_snapshots().iloc[0]
    assert record.id == identity and pd.isna(record.opportunity_score)
    assert record.entry_score == 85
    with journal._connect() as connection:
        score = next(row for row in connection.execute("PRAGMA table_info(analysis_snapshots)")
                     if row["name"] == "opportunity_score")
        assert score["notnull"] == 0
        assert connection.execute("SELECT opportunity_score IS NULL FROM analysis_snapshots").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE analysis_snapshots SET opportunity_score=101")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE analysis_snapshots SET opportunity_score=-1")


@pytest.mark.parametrize("force_legacy_runtime", [False, True])
def test_legacy_migration_preserves_rows_custom_objects_foreign_keys_and_sequence(tmp_path, monkeypatch, force_legacy_runtime):
    path = tmp_path / "legacy.sqlite"
    legacy_database(path)
    before = preserved_state(path)
    if force_legacy_runtime:
        monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 52, 0))
    journal = TradingJournal(path, owner="synthetic")
    assert preserved_state(path) == before
    with journal._connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM snapshot_insert_log").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM snapshot_custom_view").fetchone()[0] == 2
        score = next(row for row in connection.execute("PRAGMA table_info(analysis_snapshots)")
                     if row["name"] == "opportunity_score")
        assert score["notnull"] == 0
        version = connection.execute("PRAGMA schema_version").fetchone()[0]
    TradingJournal(path, owner="synthetic")
    with journal._connect() as connection:
        assert connection.execute("PRAGMA schema_version").fetchone()[0] == version
    identity = journal.add_analysis_snapshot(**snapshot())
    assert identity == 801
    with journal._connect() as connection:
        assert connection.execute("SELECT snapshot_id FROM snapshot_insert_log").fetchone()[0] == identity
        assert connection.execute("SELECT doubled_score FROM snapshot_custom_view WHERE id=?", (identity,)).fetchone()[0] == 170
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE analysis_snapshots SET opportunity_score=101 WHERE id=?", (identity,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO snapshot_child VALUES (999999)")


def test_legacy_migration_failure_rolls_back_schema_rows_objects_and_sequence(tmp_path, monkeypatch):
    path = tmp_path / "rollback.sqlite"
    legacy_database(path)
    before = preserved_state(path)
    with connect(path) as connection:
        schema = connection.execute("SELECT sql FROM sqlite_schema WHERE name='analysis_snapshots'").fetchone()[0]
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 52, 0))

    class FailingConnection(sqlite3.Connection):
        def execute(self, query, *args, **kwargs):
            if query.startswith('ALTER TABLE "__analysis_snapshots_nullable_v1" RENAME'):
                raise sqlite3.OperationalError("Synthetic failure after dropping the old table")
            return super().execute(query, *args, **kwargs)

    def broken_connect(self):
        connection = sqlite3.connect(self.database_path, factory=FailingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(TradingJournal, "_connect", broken_connect)
        with pytest.raises(sqlite3.OperationalError):
            TradingJournal(path, owner="synthetic")
    assert preserved_state(path) == before
    with connect(path) as connection:
        assert connection.execute("SELECT sql FROM sqlite_schema WHERE name='analysis_snapshots'").fetchone()[0] == schema
        assert connection.execute("SELECT 1 FROM sqlite_schema WHERE name='__analysis_snapshots_nullable_v1'").fetchone() is None
    # A later attempt can safely complete after the transient failure.
    TradingJournal(path, owner="synthetic")
    assert preserved_state(path) == before


def test_migration_never_overwrites_an_existing_temporary_name(tmp_path, monkeypatch):
    path = tmp_path / "collision.sqlite"
    legacy_database(path)
    before = preserved_state(path)
    with connect(path) as connection:
        connection.execute("CREATE TABLE __analysis_snapshots_nullable_v1(value TEXT)")
        connection.execute("INSERT INTO __analysis_snapshots_nullable_v1 VALUES ('preserve')")
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 52, 0))
    with pytest.raises(ValueError, match="already in use"):
        TradingJournal(path)
    assert preserved_state(path) == before
    with connect(path) as connection:
        assert connection.execute("SELECT value FROM __analysis_snapshots_nullable_v1").fetchone()[0] == "preserve"


@pytest.mark.parametrize("declaration", [
    'opportunity_score INTEGER NOT NULL CHECK(opportunity_score BETWEEN 0 AND 100)',
    '"opportunity_score" INTEGER DEFAULT 0 CONSTRAINT score_required NOT NULL ON CONFLICT FAIL CHECK("opportunity_score" BETWEEN 0 AND 100)',
])
def test_ddl_rewrite_only_changes_optional_constraint_not_literal_text(declaration):
    original = f"CREATE TABLE analysis_snapshots(id INTEGER PRIMARY KEY, {declaration}, custom_text TEXT DEFAULT 'NOT NULL, opportunity_score INTEGER NOT NULL')"
    changed = TradingJournal._nullable_snapshot_ddl(original, "replacement")
    assert "DEFAULT 'NOT NULL, opportunity_score INTEGER NOT NULL'" in changed
    with sqlite3.connect(":memory:") as connection:
        connection.execute(changed)
        connection.execute("INSERT INTO replacement(id,opportunity_score) VALUES (1,NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO replacement(id,opportunity_score) VALUES (2,101)")
