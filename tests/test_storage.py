from src.storage import GROUP_PORTFOLIO_OWNER, create_journal, load_supabase_config


def test_supabase_config_can_come_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setenv("SUPABASE_TABLE", "operations")
    monkeypatch.setenv("SUPABASE_FAVORITES_TABLE", "favorites")

    config = load_supabase_config()

    assert config is not None
    assert config.url == "https://example.supabase.co"
    assert config.secret_key == "sb_secret_test"
    assert config.table == "operations"
    assert config.favorites_table == "favorites"


def test_local_journals_are_isolated_by_owner(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.storage.load_supabase_config", lambda: None)
    monkeypatch.setenv("STOCK_SIGNAL_LAB_DATA_DIR", str(tmp_path))

    first = create_journal("usuario1")
    second = create_journal("usuario2")
    first.add_operation("ABC", "Compra", 1, 10, 0, "2025-01-01")

    assert len(first.list_operations()) == 1
    assert second.list_operations().empty
    assert first.database_path != second.database_path


def test_group_portfolio_is_shared_without_mixing_private_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.storage.load_supabase_config", lambda: None)
    monkeypatch.setenv("STOCK_SIGNAL_LAB_DATA_DIR", str(tmp_path))

    group_for_luci = create_journal(GROUP_PORTFOLIO_OWNER)
    group_for_fer = create_journal(GROUP_PORTFOLIO_OWNER)
    private_luci = create_journal("luci")
    group_for_luci.add_operation(
        "ABC",
        "Compra",
        1,
        10,
        0,
        "2025-01-01",
        recorded_by="luci",
    )

    assert len(group_for_fer.list_operations()) == 1
    assert group_for_fer.list_operations().iloc[0]["recorded_by"] == "luci"
    assert private_luci.list_operations().empty


def test_group_favorites_are_shared_without_mixing_private_list(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.storage.load_supabase_config", lambda: None)
    monkeypatch.setenv("STOCK_SIGNAL_LAB_DATA_DIR", str(tmp_path))

    group_for_luci = create_journal(GROUP_PORTFOLIO_OWNER)
    group_for_fer = create_journal(GROUP_PORTFOLIO_OWNER)
    private_luci = create_journal("luci")
    group_for_luci.add_favorite(
        "TSM",
        "Taiwan Semiconductor",
        "NYSE",
        recorded_by="luci",
    )

    assert group_for_fer.list_favorites()["ticker"].tolist() == ["TSM"]
    assert private_luci.list_favorites().empty
