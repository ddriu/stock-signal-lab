"""Acceso multiusuario sencillo para despliegues privados pequeños.

Las contraseñas nunca se guardan en claro: se verifican mediante
PBKDF2-HMAC-SHA256. Los roles se configuran en los secretos del servidor y cada
cuenta utiliza su nombre como propietario aislado del diario de operaciones.
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
    role: str = "user"
    display_name: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


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


def _normalize_account(
    username: str,
    values: object,
) -> AuthConfig | None:
    """Valida una cuenta procedente de TOML sin aceptar roles arbitrarios."""

    if not isinstance(values, dict):
        try:
            values = dict(values)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    normalized_username = username.strip().lower()
    if (
        not normalized_username
        or len(normalized_username) > 40
        or not normalized_username.replace("_", "").replace("-", "").isalnum()
    ):
        return None
    password_hash = str(values.get("password_hash", "")).strip()
    role = str(values.get("role", "user")).strip().lower()
    if not password_hash or role not in {"user", "admin"}:
        return None
    display_name = str(values.get("display_name", normalized_username)).strip()
    return AuthConfig(
        username=normalized_username,
        password_hash=password_hash,
        role=role,
        display_name=display_name or normalized_username,
    )


def load_auth_accounts() -> dict[str, AuthConfig]:
    """Carga todas las cuentas desde secretos, conservando el formato antiguo."""

    accounts: dict[str, AuthConfig] = {}
    users_section = _secret_section("users")
    for username, values in users_section.items():
        account = _normalize_account(str(username), values)
        if account is not None:
            accounts[account.username] = account

    if accounts:
        return accounts

    # Compatibilidad con instalaciones que todavía usan una sola cuenta.
    legacy = _secret_section("app_auth")
    username = str(
        legacy.get("username")
        or os.getenv("STOCK_SIGNAL_LAB_USERNAME", "")
    ).strip()
    password_hash = str(
        legacy.get("password_hash")
        or os.getenv("STOCK_SIGNAL_LAB_PASSWORD_HASH", "")
    ).strip()
    if username and password_hash:
        account = _normalize_account(
            username,
            {
                "password_hash": password_hash,
                "role": str(legacy.get("role", "user")),
                "display_name": str(legacy.get("display_name", username)),
            },
        )
        if account is not None:
            accounts[account.username] = account
    return accounts


def load_auth_config() -> AuthConfig | None:
    """Compatibilidad: devuelve la primera cuenta configurada."""

    accounts = load_auth_accounts()
    return next(iter(accounts.values()), None)


def managed_usernames(accounts: dict[str, AuthConfig]) -> list[str]:
    """Devuelve las cuentas de inversión visibles en el panel administrador."""

    return sorted(
        account.username
        for account in accounts.values()
        if not account.is_admin
    )


def persistent_journal_enabled() -> bool:
    """Indica si el anfitrión garantiza almacenamiento local persistente."""

    supabase = _secret_section("supabase")
    supabase_url = str(
        supabase.get("url") or os.getenv("SUPABASE_URL", "")
    ).strip()
    supabase_key = str(
        supabase.get("secret_key")
        or supabase.get("service_role_key")
        or os.getenv("SUPABASE_SECRET_KEY", "")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    ).strip()
    if supabase_url and supabase_key:
        return True

    section = _secret_section("deployment")
    configured = section.get("persistent_journal")
    if configured is None:
        return os.getenv("STOCK_SIGNAL_LAB_PERSISTENT_JOURNAL", "true").lower() in {
            "1",
            "true",
            "yes",
        }
    return bool(configured)


def require_login() -> AuthConfig:
    """Muestra la puerta de acceso y detiene la app hasta autenticar al usuario."""

    accounts = load_auth_accounts()
    if not accounts:
        st.error(
            "La aplicación está cerrada porque no se han configurado credenciales. "
            "Añade las cuentas [users.nombre] en los secretos de Streamlit."
        )
        st.stop()

    authenticated_user = st.session_state.get("_authenticated_user")
    account = accounts.get(str(authenticated_user).lower())
    if account is not None:
        role_text = "Administrador" if account.is_admin else "Usuario"
        st.sidebar.caption(f"Sesión: {account.display_name} · {role_text}")
        if st.sidebar.button("Cerrar sesión", width="stretch"):
            st.session_state.pop("_authenticated_user", None)
            st.rerun()
        return account

    st.markdown(
        """
        <div class="ssl-login-brand">
            <div class="ssl-logo" aria-hidden="true">↗</div>
            <h1>Stock Signal Lab</h1>
            <p>Decisiones de inversión explicadas con datos, riesgo y contexto.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.form("login_form"):
        username = st.text_input("Usuario", autocomplete="username")
        password = st.text_input(
            "Contraseña",
            type="password",
            autocomplete="current-password",
        )
        submitted = st.form_submit_button("Entrar", type="primary", width="stretch")
    if submitted:
        normalized_username = username.strip().lower()
        candidate = accounts.get(normalized_username)
        valid_password = bool(
            candidate and verify_password(password, candidate.password_hash)
        )
        if candidate and valid_password:
            st.session_state["_authenticated_user"] = candidate.username
            st.rerun()
        st.error("Usuario o contraseña incorrectos.")
    st.markdown(
        """
        <p style="max-width:470px;margin:0.8rem auto;text-align:center;color:#64748b;font-size:.82rem">
        Acceso privado. La contraseña se verifica de forma segura y no aparece en el código.
        </p>
        """,
        unsafe_allow_html=True,
    )
    st.stop()
    return AuthConfig("", "")  # Ayuda al analizador; st.stop() interrumpe.
