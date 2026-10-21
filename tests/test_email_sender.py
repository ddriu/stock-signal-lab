from __future__ import annotations

from email import policy
from email.parser import BytesParser
from dataclasses import asdict, FrozenInstanceError
from datetime import datetime, timezone
import re

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

    receipt = send_email("test@example.invalid", subject, text, html, config=config)

    assert len(captured) == 1
    assert receipt is None  # A legacy fake returning None is not an SMTP receipt.
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


@pytest.mark.parametrize("use_ssl", [True, False])
def test_empty_smtp_refusal_dictionary_creates_private_acceptance_receipt(monkeypatch, use_ssl) -> None:
    called = []

    class AcceptingSMTP:
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
            called.append("accepted")
            return {}

    monkeypatch.setattr(email_sender.smtplib, "SMTP_SSL", AcceptingSMTP)
    monkeypatch.setattr(email_sender.smtplib, "SMTP", AcceptingSMTP)
    config = EmailConfig(host="smtp.example.invalid", port=465, username="private-account",
                         password="private-password", sender_email="sender@example.invalid",
                         use_ssl=use_ssl)
    before = datetime.now(timezone.utc)
    receipt = send_email("recipient@example.invalid", "Private subject", "Private body ñ €",
                         "<p>Private body ñ €</p>", config=config)
    after = datetime.now(timezone.utc)
    assert called == ["accepted"]
    assert isinstance(receipt, email_sender.EmailDeliveryReceipt)
    assert receipt.status == "smtp_accepted" and receipt.smtp_code == 250
    assert before <= datetime.fromisoformat(receipt.accepted_at) <= after
    assert re.fullmatch(r"[0-9a-f]{64}", receipt.receipt_hash)
    assert re.fullmatch(r"[0-9a-f]{64}", receipt.message_hash)
    assert not {"delivered", "read", "recipient", "sender", "password"} & asdict(receipt).keys()
    for secret in ("private-account", "private-password", "example.invalid", "Private subject", "Private body"):
        assert secret not in repr(receipt)
    with pytest.raises(FrozenInstanceError):
        receipt.status = "delivered"
    another = send_email("recipient@example.invalid", "Private subject", "Private body ñ €",
                         "<p>Private body ñ €</p>", config=config)
    assert another.message_hash == receipt.message_hash
    changed = send_email("recipient@example.invalid", "Changed subject", "Private body ñ €",
                         "<p>Private body ñ €</p>", config=config)
    assert changed.message_hash != receipt.message_hash


def test_smtp_transport_failure_never_constructs_an_acceptance_receipt(monkeypatch) -> None:
    class BrokenSMTP:
        def __init__(self, *args, **kwargs):
            raise OSError("synthetic failure")

    monkeypatch.setattr(email_sender.smtplib, "SMTP_SSL", BrokenSMTP)
    monkeypatch.setattr(email_sender, "_acceptance_receipt", lambda *args: pytest.fail("No acceptance occurred"))
    config = EmailConfig(host="smtp.example.invalid", port=465, username="demo",
                         password="private-password", sender_email="sender@example.invalid")
    with pytest.raises(email_sender.EmailDeliveryError, match="No se pudo enviar"):
        send_email("recipient@example.invalid", "Test", "Text", "<p>Text</p>", config=config)


def test_quit_failure_preserves_known_data_acceptance_without_swallowing_original_error(monkeypatch) -> None:
    class AcceptedThenDisconnectedSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            raise email_sender.smtplib.SMTPServerDisconnected("synthetic QUIT failure")

        def login(self, *args):
            pass

        def send_message(self, message):
            return {}

    monkeypatch.setattr(email_sender.smtplib, "SMTP_SSL", AcceptedThenDisconnectedSMTP)
    config = EmailConfig(host="smtp.example.invalid", port=465, username="demo",
                         password="private-password", sender_email="sender@example.invalid")
    with pytest.raises(email_sender.EmailDeliveryError) as failure:
        send_email("recipient@example.invalid", "Test", "Text", "<p>Text</p>", config=config)
    assert isinstance(failure.value.__cause__, email_sender.smtplib.SMTPServerDisconnected)
    assert failure.value.smtp_receipt.status == "smtp_accepted"
    assert failure.value.smtp_receipt.smtp_code == 250
