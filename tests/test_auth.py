from src.auth import hash_password, verify_password


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
