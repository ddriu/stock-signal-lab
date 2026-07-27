from src.auth import (
    hash_password,
    load_auth_accounts,
    managed_usernames,
    verify_password,
)


def test_password_hash_round_trip() -> None:
    encoded = hash_password(
        "una-contraseña-segura",
        salt=b"0123456789abcdef",
    )
    assert verify_password("una-contraseña-segura", encoded)
    assert not verify_password("otra-contraseña", encoded)
    assert "una-contraseña-segura" not in encoded


def test_malformed_password_hash_is_rejected() -> None:
    assert not verify_password("anything", "not-a-valid-hash")


def test_multiuser_config_supports_roles_and_display_names(monkeypatch) -> None:
    password_hash = hash_password("segura", salt=b"0123456789abcdef")
    sections = {
        "users": {
            "alberite": {
                "password_hash": password_hash,
                "role": "admin",
                "display_name": "Alberite",
            },
            "usuario1": {
                "password_hash": password_hash,
                "role": "user",
                "display_name": "Usuario 1",
            },
        }
    }
    monkeypatch.setattr(
        "src.auth._secret_section",
        lambda name: sections.get(name, {}),
    )

    accounts = load_auth_accounts()

    assert accounts["alberite"].is_admin
    assert accounts["usuario1"].display_name == "Usuario 1"
    assert managed_usernames(accounts) == ["usuario1"]


def test_invalid_roles_are_not_loaded(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.auth._secret_section",
        lambda name: {
            "intruso": {
                "password_hash": "hash",
                "role": "superadmin",
            }
        }
        if name == "users"
        else {},
    )

    assert load_auth_accounts() == {}
