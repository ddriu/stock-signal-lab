from __future__ import annotations

from email import policy
from email.parser import BytesParser

import pytest

from src import email_sender
from src.email_sender import EmailConfig, send_email


@pytest.mark.parametrize("use_ssl", [True, False])
def test_smtp_mime_round_trip_preserves_spanish_and_currency(monkeypatch, use_ssl) -> None:
    """Simula SMTP: no conecta ni entrega ningún mensaje real."""

    captured = []

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def starttls(self, **kwargs):
            pass

        def login(self, *args):
            pass

        def send_message(self, message):
            captured.append(message.as_bytes())

    monkeypatch.setattr(email_sender.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(email_sender.smtplib, "SMTP", FakeSMTP)
    text = "Qué cambió: revisión, decisión, señal, año, inversión y 10 € · áéíóúüñ."
    html = f'<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"></head><body>{text}</body></html>'
    subject = "Revisión de cartera · Fermín"
    config = EmailConfig(
        host="smtp.example.invalid",
        port=465 if use_ssl else 587,
        username="demo",
        password="fake-only-for-test",
        sender_email="demo@example.invalid",
        sender_name="María",
        use_ssl=use_ssl,
    )

    send_email("test@example.invalid", subject, text, html, config=config)

    assert len(captured) == 1
    message = BytesParser(policy=policy.default).parsebytes(captured[0])
    assert str(message["Subject"]) == subject
    assert "María" in str(message["From"])
    parts = list(message.iter_parts())
    assert [part.get_content_type() for part in parts] == ["text/plain", "text/html"]
    assert all(part.get_content_charset() == "utf-8" for part in parts)
    assert parts[0].get_content().strip() == text
    assert parts[1].get_content().strip() == html


@pytest.mark.parametrize("use_ssl", [True, False])
def test_smtp_refusal_is_not_reported_as_acceptance(monkeypatch, use_ssl) -> None:
    class RefusingSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def login(self, *args):
            pass

        def starttls(self, **kwargs):
            pass

        def send_message(self, message):
            return {"test@example.invalid": (550, b"Mailbox unavailable")}

    monkeypatch.setattr(email_sender.smtplib, "SMTP_SSL", RefusingSMTP)
    monkeypatch.setattr(email_sender.smtplib, "SMTP", RefusingSMTP)
    config = EmailConfig(host="smtp.example.invalid", port=465, username="demo",
                         password="fake-only-for-test", sender_email="demo@example.invalid",
                         use_ssl=use_ssl)
    with pytest.raises(email_sender.EmailDeliveryError, match="rechazó el destinatario"):
        send_email("test@example.invalid", "Prueba", "Texto", "<p>Texto</p>", config=config)
