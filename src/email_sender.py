"""Envío SMTP configurable para Gmail y futuros proveedores."""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
import os
import smtplib
import ssl


class EmailConfigurationError(RuntimeError):
    """Configuración incompleta o insegura del proveedor de correo."""


class EmailDeliveryError(RuntimeError):
    """El servidor SMTP rechazó o no pudo entregar el mensaje."""


@dataclass(frozen=True)
class EmailConfig:
    host: str
    port: int
    username: str
    password: str
    sender_email: str
    sender_name: str = "Stock Signal Lab"
    use_ssl: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.host and self.port and self.username and self.password)


def _streamlit_email_secrets() -> dict[str, object]:
    try:
        import streamlit as st

        section = st.secrets.get("email", {})
        return dict(section) if section else {}
    except (ImportError, FileNotFoundError, RuntimeError):
        return {}


def load_email_config() -> EmailConfig:
    """Lee secretos de Streamlit o variables de entorno de GitHub Actions."""

    section = _streamlit_email_secrets()
    host = str(
        section.get("smtp_host")
        or os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
    ).strip()
    port = int(section.get("smtp_port") or os.getenv("EMAIL_SMTP_PORT", "465"))
    username = str(
        section.get("username") or os.getenv("EMAIL_SMTP_USERNAME", "")
    ).strip()
    password = str(
        section.get("app_password")
        or section.get("password")
        or os.getenv("EMAIL_SMTP_PASSWORD", "")
    ).strip()
    sender_email = str(
        section.get("sender_email")
        or os.getenv("EMAIL_SENDER", "")
        or username
    ).strip()
    sender_name = str(
        section.get("sender_name")
        or os.getenv("EMAIL_SENDER_NAME", "Stock Signal Lab")
    ).strip()
    raw_ssl = section.get("use_ssl", os.getenv("EMAIL_SMTP_USE_SSL", "true"))
    use_ssl = (
        raw_ssl
        if isinstance(raw_ssl, bool)
        else str(raw_ssl).strip().lower() in {"1", "true", "yes", "on"}
    )
    return EmailConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        sender_email=sender_email,
        sender_name=sender_name or "Stock Signal Lab",
        use_ssl=bool(use_ssl),
    )


def send_email(
    recipient: str,
    subject: str,
    plain_body: str,
    html_body: str,
    *,
    config: EmailConfig | None = None,
) -> None:
    """Envía un mensaje multipart; nunca registra ni devuelve la contraseña."""

    resolved = config or load_email_config()
    if not resolved.configured or not resolved.sender_email:
        raise EmailConfigurationError(
            "El correo automático aún no está conectado por el administrador."
        )
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{resolved.sender_name} <{resolved.sender_email}>"
    message["To"] = recipient
    message.set_content(plain_body)
    message.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    try:
        if resolved.use_ssl:
            with smtplib.SMTP_SSL(
                resolved.host,
                resolved.port,
                context=context,
                timeout=30,
            ) as server:
                server.login(resolved.username, resolved.password)
                server.send_message(message)
        else:
            with smtplib.SMTP(resolved.host, resolved.port, timeout=30) as server:
                server.starttls(context=context)
                server.login(resolved.username, resolved.password)
                server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError(
            "No se pudo enviar el correo. Revisa la cuenta y su contraseña de aplicación."
        ) from exc


def send_test_email(recipient: str, *, config: EmailConfig | None = None) -> None:
    send_email(
        recipient,
        "Stock Signal Lab · correo de prueba",
        (
            "El correo de Stock Signal Lab está conectado correctamente.\n\n"
            "Las alertas son informativas y no constituyen asesoramiento financiero."
        ),
        """
        <div style="font-family:Arial,sans-serif;max-width:620px;margin:auto">
          <h2>Stock Signal Lab</h2>
          <p>El correo de prueba se ha enviado correctamente.</p>
          <p style="color:#64748b;font-size:12px">
            Las alertas son informativas y no constituyen asesoramiento financiero.
          </p>
        </div>
        """,
        config=config,
    )
