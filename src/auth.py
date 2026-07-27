"""Acceso sencillo por contraseña para despliegues privados pequeños.

La contraseña nunca se guarda en claro: se verifica mediante PBKDF2-HMAC-SHA256.
Este acceso compartido es apropiado para una app familiar o de demostración. Para
usuarios independientes y recuperación de contraseñas debe sustituirse por OIDC
o un servicio de identidad.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import hmac
import os

import streamlit as st


HASH_ALGORITHM = "pbkdf2_sha256"


@dataclass(frozen=True)
class AuthConfig:
    username: str
    password_hash: str


def hash_password(
    password: str,
    *,
    iterations: int = 600_000,
    salt: bytes | None = None,
) -> str:
    """Crea un hash portable; se expone también para rotar la contraseña."""

    if not password:
        raise ValueError("La contraseña no puede estar vacía.")
    if iterations < 100_000:
        raise ValueError("El número de iteraciones es insuficiente.")
    resolved_salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        resolved_salt,
        iterations,
    )
    return "$".join(
        (
            HASH_ALGORITHM,
            str(iterations),
            base64.urlsafe_b64encode(resolved_salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded_hash: str) -> bool:
    """Verifica una contraseña sin comparaciones vulnerables a temporización."""

    try:
        algorithm, iterations_text, salt_text, expected_text = encoded_hash.split("$", 3)
        if algorithm != HASH_ALGORITHM:
            return False
        iterations = int(iterations_text)
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(expected_text.encode("ascii"))
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(candidate, expected)


def _secret_section(name: str) -> dict[str, object]:
    try:
        section = st.secrets.get(name, {})
    except (FileNotFoundError, RuntimeError):
        return {}
    return dict(section) if section else {}


def load_auth_config() -> AuthConfig | None:
    """Carga credenciales desde secretos de Streamlit o variables de entorno."""

    section = _secret_section("app_auth")
    username = str(
        section.get("username")
        or os.getenv("STOCK_SIGNAL_LAB_USERNAME", "")
    ).strip()
    password_hash = str(
        section.get("password_hash")
        or os.getenv("STOCK_SIGNAL_LAB_PASSWORD_HASH", "")
    ).strip()
    if not username or not password_hash:
        return None
    return AuthConfig(username=username, password_hash=password_hash)


def persistent_journal_enabled() -> bool:
    """Indica si el anfitrión garantiza almacenamiento local persistente."""

    section = _secret_section("deployment")
    configured = section.get("persistent_journal")
    if configured is None:
        return os.getenv("STOCK_SIGNAL_LAB_PERSISTENT_JOURNAL", "true").lower() in {
            "1",
            "true",
            "yes",
        }
    return bool(configured)


def require_login() -> str:
    """Muestra la puerta de acceso y detiene la app hasta autenticar al usuario."""

    config = load_auth_config()
    if config is None:
        st.error(
            "La aplicación está cerrada porque no se han configurado credenciales. "
            "Añade [app_auth] en los secretos de Streamlit."
        )
        st.stop()

    authenticated_user = st.session_state.get("_authenticated_user")
    if authenticated_user == config.username:
        st.sidebar.caption(f"Sesión: {config.username}")
        if st.sidebar.button("Cerrar sesión", width="stretch"):
            st.session_state.pop("_authenticated_user", None)
            st.rerun()
        return config.username

    st.title("Stock Signal Lab")
    st.caption("Acceso privado")
    with st.form("login_form"):
        username = st.text_input("Usuario", autocomplete="username")
        password = st.text_input(
            "Contraseña",
            type="password",
            autocomplete="current-password",
        )
        submitted = st.form_submit_button("Entrar", type="primary", width="stretch")
    if submitted:
        valid_user = hmac.compare_digest(username.strip(), config.username)
        valid_password = verify_password(password, config.password_hash)
        if valid_user and valid_password:
            st.session_state["_authenticated_user"] = config.username
            st.rerun()
        st.error("Usuario o contraseña incorrectos.")
    st.caption(
        "La contraseña se comprueba mediante un hash seguro y no aparece en el código."
    )
    st.stop()
    return ""  # Ayuda al analizador de tipos; st.stop() interrumpe la ejecución.
