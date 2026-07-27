from src.storage import load_supabase_config


def test_supabase_config_can_come_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setenv("SUPABASE_TABLE", "operations")

    config = load_supabase_config()

    assert config is not None
    assert config.url == "https://example.supabase.co"
    assert config.secret_key == "sb_secret_test"
    assert config.table == "operations"
