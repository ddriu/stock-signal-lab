from src.storage import create_journal, load_supabase_config


def test_supabase_config_can_come_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setenv("SUPABASE_TABLE", "operations")

    config = load_supabase_config()

    assert config is not None
    assert config.url == "https://example.supabase.co"
    assert config.secret_key == "sb_secret_test"
    assert config.table == "operations"


def test_local_journals_are_isolated_by_owner(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.storage.load_supabase_config", lambda: None)
    monkeypatch.setenv("STOCK_SIGNAL_LAB_DATA_DIR", str(tmp_path))

    first = create_journal("usuario1")
    second = create_journal("usuario2")
    first.add_operation("ABC", "Compra", 1, 10, 0, "2025-01-01")

    assert len(first.list_operations()) == 1
    assert second.list_operations().empty
    assert first.database_path != second.database_path
