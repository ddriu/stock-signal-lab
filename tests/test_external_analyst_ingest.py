"""Synthetic private email fixtures; never a real message, mailbox or network."""
from dataclasses import replace
from email.message import EmailMessage
import hashlib
import json
from types import SimpleNamespace
import sys

import pytest

from src import external_analyst_ingest as ingest
from src.email_sender import EmailConfig


SENDER = "radar@peritoinversor.com"
RECEIVED = "2026-11-12T08:00:00+00:00"
OBSERVED = "2026-11-12T08:30:00+00:00"
FIRST = """Empresa: Empresa sintética Alfa
Ticker: NASDAQ:QAAA
Divisa: USD
Analista: Ana Sintética
Fecha del pronóstico: 10/11/2026
Recomendación: Comprar
Entrada: 100,50 USD
Stop loss: 95,20 USD
Objetivo: 115,75 USD
Precio de referencia: 101,10 USD
Estado: Pendiente
Tipo de entrada: Límite
Ficha: https://www.peritoinversor.com/ficha/alfa
"""
SECOND = """Empresa: Empresa sintética Beta
Ticker: QBBB
Mercado: NYSE
Divisa: USD
Analista: Beatriz Sintética
Fecha del pronóstico: 2026-10-01T12:00:00+02:00
Recomendación: Comprar
Entrada: 10
Stop loss: 9
Objetivo: 12
Precio de referencia: 13
Estado: Objetivo alcanzado
Tipo de entrada: Ruptura
Ficha: https://peritoinversor.com/ficha/beta
"""


def email(body=FIRST + "\n" + SECOND, *, html=False, sender=SENDER, auth=True, subject="71 sugerencias para seguir"):
    message = EmailMessage()
    message["From"] = f"Perito Inversor <{sender}>"
    message["Subject"] = subject
    message["Message-ID"] = "<synthetic-private@example.invalid>"
    message["Date"] = "Thu, 12 Nov 2026 08:00:00 +0000"
    if auth:
        message["Authentication-Results"] = "mx.google.com; dkim=pass header.i=@peritoinversor.com"
    message.set_content(body, subtype="html" if html else "plain")
    return message.as_bytes()


def parse(raw, **kwargs):
    return ingest.parse_email_bytes(raw, received_at=RECEIVED, observed_at=OBSERVED,
                                   allowed_senders=(SENDER,), **kwargs)


def test_announced_count_never_creates_absent_recommendations_or_historical_gains():
    result = parse(email())
    assert result["announced_count"] == 71
    assert result["extracted_count"] == len(result["calls"]) == 2
    assert "announced_count_differs_from_extracted" in result["issues"]
    first, historical = result["calls"]
    assert first["ticker"] == "QAAA" and first["exchange"] == "NASDAQ"
    assert first["entry"] == 100.5 and first["stop"] == 95.2 and first["target"] == 115.75
    assert first["published_at"] is None
    assert first["evidence"]["published_date"] == "2026-11-10"
    assert first["evidence"]["published_precision"] == "day"
    assert first["received_at"] == RECEIVED and first["observed_at"] == OBSERVED
    assert first["executable"]
    assert historical["source_status"] == "target_reached" and not historical["executable"]
    assert historical["published_at"] == "2026-10-01T10:00:00+00:00"
    assert result["authentication"] == "manual_unverified"
    assert "raw" not in result and "body" not in result
    assert "Empresa sintética" not in str({k: v for k, v in result.items() if k != "calls"})


def test_html_table_links_unicode_and_scripts_are_not_executed():
    html = """<html><head><title>Secret</title><script>network.fetch('https://evil.invalid')</script></head>
    <body><table><tr><th>Empresa</th><th>Ticker</th><th>Divisa</th><th>Analista</th><th>Entrada</th>
    <th>Stop loss</th><th>Objetivo</th><th>Recomendación</th><th>Tipo de entrada</th><th>Ficha</th></tr>
    <tr><td>Compañía sintética €</td><td>QCCC</td><td>EUR</td><td>Óscar</td><td>1.234,56 €</td>
    <td>1.200,00 €</td><td>1.300,00 €</td><td>Comprar</td><td>A mercado</td>
    <td><a href="https://peritoinversor.com/ficha/qccc">Ver ficha</a></td></tr></table></body></html>"""
    result = parse(email(html, html=True, subject="Una sugerencia"))
    assert result["extracted_count"] == 1
    call = result["calls"][0]
    assert call["company_name"] == "Compañía sintética €" and call["analyst"] == "Óscar"
    assert call["entry"] == 1234.56 and call["entry_rule"] == "market"
    assert call["source_url"] == "https://peritoinversor.com/ficha/qccc"
    assert "evil.invalid" not in str(result)


def test_observed_newsletter_card_shape_is_synthetic_and_does_not_invent_entries():
    # Schema-shaped synthetic fixture, not the user's private newsletter.
    def card(ticker, company, marker, state, fragment):
        return f'''<table><tr><td class="pi-ficha"><p><strong>{ticker}</strong> · {company}</p>
        <p><span>▲ Alcista</span> · Sugerencia · Ana Sintética · Entidad Pruebas · 10 de noviembre de 2026</p>
        <table><tr><td class="pi-aviso"><strong>{state}</strong> Narrative must not create operations.</td></tr></table>
        <p>Information, not an executable entry rule.</p><table>
        <tr><td>Entrada</td><td>{marker}100,00</td></tr>
        <tr><td>Stop loss</td><td>{marker}95,00</td></tr>
        <tr><td>Objetivo</td><td>{marker}110,00</td></tr>
        <tr><td>Precio actual</td><td>{marker}112,00 (+12 % vs. entrada)</td></tr></table>
        <p><a href="https://www.peritoinversor.com/valor/{ticker.lower()}/#{fragment}">Ver la ficha completa</a></p>
        </td></tr></table>'''
    html = card("QDDD", "Sintética Delta", "$", "Objetivo alcanzado.", "ins-ana-qddd-20261110")
    html += card("QEEE", "Sintética Épsilon", "€", "Salta el stop loss.", "ins-ana-qeee-20261110")
    html += '<a href="https://www.peritoinversor.com/.netlify/functions/unsubscribe?email=private%40example.invalid&amp;t=private-token">Cancelar</a>'
    result = parse(email(html, html=True, subject="[Prueba] 71 nuevas sugerencias de inversión"))
    assert result["announced_count"] == 71 and result["extracted_count"] == 2
    usd, eur = result["calls"]
    assert usd["currency"] is None and usd["evidence"]["ambiguous_currency_marker"] == "$"
    assert eur["currency"] == "EUR"
    assert usd["analyst"] == "Ana Sintética" and usd["direction"] == "long"
    assert usd["reference_price"] == 112 and usd["evidence"]["published_date"] == "2026-11-10"
    assert usd["source_id"] == "ins-ana-qddd-20261110"
    assert [c["source_status"] for c in result["calls"]] == ["target_reached", "stopped"]
    assert all(not c["executable"] and c["action"] == "unknown" for c in result["calls"])
    assert "private-token" not in str(result) and "private%40" not in str(result)


def newsletter_card(index, notice="", *, opinion="", target=True, extra_rows=""):
    """Synthetic provider layout; no private mail contents or real instrument."""
    return f'''<table role="presentation"><tr><td class="pi-ficha">
    <p><strong>Q{index:02}</strong> · Empresa sintética {index}</p>
    <p>▲ Alcista · Sugerencia · Analista Sintética {index} · Organización Prueba · 10 de noviembre de 2026</p>
    <table><tr><td class="pi-aviso">{notice}</td></tr></table><p>{opinion}</p>
    <table><tr><td>Entrada</td><td>€100,00</td></tr><tr><td>Stop loss</td><td>€95,00</td></tr>
    {'<tr><td>Objetivo</td><td>€110,00</td></tr>' if target else ''}
    <tr><td>Precio actual</td><td>€102,00 (+2 % vs. entrada)</td></tr>{extra_rows}</table>
    <p><a href="https://www.peritoinversor.com/valor/q{index:02}/#ins-synthetic-q{index:02}-20261110">Ficha</a></p>
    </td></tr></table>'''


@pytest.mark.parametrize("announced,historical_count", [(71, 10), (18, 2)])
def test_newsletter_ten_visible_cards_never_invent_advertised_or_completed_entries(announced, historical_count):
    cards = []
    for index in range(10):
        status = "Salta el stop loss." if index == 0 and historical_count == 10 else "Objetivo alcanzado."
        notice = f"<strong>{status}</strong>" if index < historical_count else ""
        cards.append(newsletter_card(index, notice, target=index < 8))
    html = "".join(cards) + f"<p>Te quedan por ver en la web {announced - 10} sugerencias nuevas.</p>"
    result = parse(email(html, html=True, subject=f"{announced} nuevas sugerencias de inversión"))
    assert result["announced_count"] == announced and result["extracted_count"] == 10
    assert result["issues"] == ["announced_count_differs_from_extracted"]
    assert len([call for call in result["calls"] if call["state"] == "historical"]) == historical_count
    assert [call["target"] for call in result["calls"][-2:]] == [None, None]
    assert all(call["action"] == "unknown" and not call["executable"] for call in result["calls"])
    for index, call in enumerate(result["calls"]):
        assert call["ticker"] == f"Q{index:02}" and call["company_name"] == f"Empresa sintética {index}"
        assert call["analyst"] == f"Analista Sintética {index}"
        assert call["evidence"]["published_date"] == "2026-11-10" and call["published_at"] is None


@pytest.mark.parametrize("heading,trigger,rule", [
    ("Pendiente de activación.", "baje a", "limit"),
    ("Pendiente de activación.", "supere", "breakout"),
    ("Entrada pendiente.", "baje a", "limit"),
])
def test_explicit_pending_notice_preserves_activation_without_creating_a_buy(heading, trigger, rule):
    notice = f"<strong>{heading}</strong> Cuenta cuando {trigger} €100,00."
    result = parse(email(newsletter_card(0, notice), html=True))
    call = result["calls"][0]
    assert call["source_status"] == "pending" and call["entry_rule"] == rule
    assert call["evidence"]["activation_level"] == 100
    assert call["evidence"]["entry_rule_provenance"] == "source_activation_notice"
    assert call["action"] == "unknown" and not call["executable"]


def test_activation_words_in_opinion_do_not_become_structured_instructions():
    opinion = "Pendiente de activación. Cuenta cuando baje a €100,00."
    call = parse(email(newsletter_card(0, opinion=opinion), html=True))["calls"][0]
    assert call["source_status"] == call["entry_rule"] == "unknown"
    assert "activation_level" not in call["evidence"] and not call["executable"]


@pytest.mark.parametrize("terminal,state", [("Objetivo alcanzado.", "target_reached"),
                                           ("Salta el stop loss.", "stopped")])
def test_completed_card_can_retain_earlier_activation_without_becoming_new_entry(terminal, state):
    notice = f"<strong>{terminal}</strong><strong>Entrada activada.</strong>"
    result = parse(email(newsletter_card(0, notice), html=True))
    assert result["extracted_count"] == 1
    call = result["calls"][0]
    assert call["source_status"] == state and call["state"] == "historical" and not call["executable"]


@pytest.mark.parametrize("notice,extra_rows", [
    ("<strong>Pendiente de activación.</strong> Cuenta cuando baje a €99,00.", ""),
    ("<strong>Pendiente de activación.</strong> Cuenta cuando baje a €0,00.", ""),
    ("<strong>Pendiente de activación.</strong><strong>Entrada activada.</strong>", ""),
    ("<strong>Objetivo alcanzado.</strong><strong>Salta el stop loss.</strong>", ""),
    ("<strong>Pendiente de activación.</strong> Cuenta cuando baje a €100,00.",
     "<tr><td>Tipo entrada</td><td>Ruptura</td></tr>"),
])
def test_conflicting_activation_blocks_only_the_affected_card(notice, extra_rows):
    html = newsletter_card(0, notice, extra_rows=extra_rows) + newsletter_card(1)
    result = parse(email(html, html=True))
    assert result["extracted_count"] == 1 and result["calls"][0]["ticker"] == "Q01"
    assert "recommendation_1: conflicting_labels" in result["issues"]


def test_explicit_activation_and_equivalent_labelled_rule_agree():
    html = newsletter_card(0, "<strong>Pendiente de activación.</strong> Cuenta cuando baje a €100,00.",
                           extra_rows="<tr><td>Tipo entrada</td><td>Límite</td></tr>")
    call = parse(email(html, html=True))["calls"][0]
    assert call["entry_rule"] == "limit" and not call["executable"]


@pytest.mark.parametrize("url", [
    "https://www.peritoinversor.com/valor/qaaa/?token=private-token",
    "https://www.peritoinversor.com/.netlify/functions/unsubscribe",
    "https://www.peritoinversor.com/account",
])
def test_private_provider_links_are_never_retained_as_sources(url):
    assert ingest._public_url(url) is None


def test_unsupported_template_and_missing_fields_remain_unverifiable():
    result = parse(email("Tenemos 71 sugerencias interesantes. Pulsa aquí para verlas."))
    assert result["extracted_count"] == 0
    assert "no_supported_labelled_recommendations" in result["issues"]
    partial = parse(email("Empresa: Solo nombre\nAnalista: Ana\nRecomendación: Vigilancia"))
    assert partial["extracted_count"] == 1
    assert partial["calls"][0]["ticker"] is None and not partial["calls"][0]["executable"]
    assert partial["calls"][0]["published_at"] is None


@pytest.mark.parametrize("body", [
    FIRST.replace("100,50 USD", "100–110 USD"),
    FIRST.replace("https://www.peritoinversor.com/ficha/alfa", "https://peritoinversor.com.evil.invalid/ficha"),
    FIRST + "Stop loss: 94\n",
])
def test_ambiguous_or_unsafe_fields_do_not_become_a_call(body):
    result = parse(email(body))
    assert result["extracted_count"] == 0
    assert result["issues"]


def test_conflicting_vertical_table_labels_do_not_silently_overwrite_levels():
    html = '<table><tr><td>Empresa</td><td>Sintética</td></tr><tr><td>Stop</td><td>95</td></tr>'
    html += '<tr><td>Stop</td><td>94</td></tr></table>'
    result = parse(email(html, html=True))
    assert result["extracted_count"] == 0
    assert result["issues"]


def test_revisions_have_stable_source_identity_and_changed_revision_content():
    a, b = (parse(email(body))["calls"][0] for body in (FIRST, FIRST.replace("115,75 USD", "120 USD")))
    assert a["id"] == b["id"] and a["source_id"] == b["source_id"]
    assert a["target"] != b["target"]
    duplicate = parse(email(FIRST + "\n" + FIRST))
    assert duplicate["extracted_count"] == 1 and "duplicate_in_message" in " ".join(duplicate["issues"])


@pytest.mark.parametrize("sender", ["other@example.invalid", "radar@peritoinversor.com.evil.invalid"])
def test_sender_must_be_exact_allowlisted(sender):
    with pytest.raises(ValueError, match="remitente"):
        parse(email(sender=sender))


def test_automatic_messages_need_first_gmail_dkim_result_and_matching_domain():
    assert parse(email(), require_gmail_auth=True)["authentication"] == "gmail_dkim_aligned"
    for raw in (
        email(auth=False),
        email().replace(b"mx.google.com; dkim=pass", b"evil.invalid; dkim=pass"),
        email().replace(b"header.i=@peritoinversor.com", b"header.i=@evil.invalid"),
        email().replace(b"dkim=pass", b"dkim=fail"),
    ):
        with pytest.raises(ValueError, match="DKIM"):
            parse(raw, require_gmail_auth=True)


def test_invalid_availability_and_size_fail_closed():
    with pytest.raises(ValueError):
        ingest.parse_email_bytes(email(), received_at=RECEIVED, observed_at="2026-11-11T00:00:00Z", allowed_senders=(SENDER,))
    with pytest.raises(ValueError):
        ingest.parse_email_bytes(email(), received_at="2026-11-12", observed_at=OBSERVED, allowed_senders=(SENDER,))
    with pytest.raises(ValueError, match="tamaño"):
        parse(b"x" * (ingest.MAX_MESSAGE_BYTES + 1))


def test_msg_dependency_is_optional_and_no_fake_success_when_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "extract_msg", None)
    with pytest.raises(ValueError, match="extract-msg==0.58.2"):
        ingest.parse_msg_bytes(b"synthetic-msg", received_at=RECEIVED, observed_at=OBSERVED, allowed_senders=(SENDER,))


def test_msg_adapter_reads_in_memory_and_never_extracts_attachments(monkeypatch):
    class SyntheticMSG:
        header = {"From": SENDER, "Subject": "71 nuevas\r\n sugerencias"}
        sender, subject, body, htmlBody = SENDER, "71 sugerencias", FIRST, None
        def __enter__(self): return self
        def __exit__(self, *_): pass
        @property
        def attachments(self): pytest.fail("No attachment extraction")
    def open_msg(raw, **kwargs):
        assert raw == b"synthetic-msg" and kwargs == {"delayAttachments": True}
        return SyntheticMSG()
    monkeypatch.setitem(sys.modules, "extract_msg", SimpleNamespace(openMsg=open_msg))
    a = ingest.parse_msg_bytes(b"synthetic-msg", received_at=RECEIVED, observed_at=OBSERVED, allowed_senders=(SENDER,))
    b = ingest.parse_msg_bytes(b"synthetic-msg", received_at=RECEIVED, observed_at=OBSERVED, allowed_senders=(SENDER,))
    assert a["message_id"] == b["message_id"] and a["calls"][0]["id"] == b["calls"][0]["id"]
    assert a["announced_count"] == 71


def test_reception_is_disabled_by_default_and_reuses_existing_account_only(monkeypatch):
    monkeypatch.setattr("src.email_sender.load_email_config", lambda: pytest.fail("Disabled means no credentials read"))
    assert not ingest.IMAPConfig.from_existing_mail(env={}).enabled
    existing = EmailConfig("smtp.gmail.com", 465, "existing@example.invalid", "private-password", "existing@example.invalid")
    config = ingest.IMAPConfig.from_existing_mail(existing, env={
        "EXTERNAL_ANALYSTS_IMAP_ENABLED": "true", "EXTERNAL_ANALYSTS_MAILBOX_OWNER": "private-owner",
        "PERITO_ALLOWED_SENDERS": SENDER, "EXTERNAL_ANALYSTS_IMAP_FOLDER": "",
    })
    assert config.username == existing.username and config.password == existing.password
    assert config.folder == "INBOX"
    assert "private-password" not in repr(config) and "existing@example.invalid" not in repr(config)
    assert ingest.poll_imap(ingest.IMAPConfig(), observed_at=OBSERVED,
                            client_factory=lambda *a, **k: pytest.fail("No connection"))["status"] == "disabled"


class FakeIMAP:
    def __init__(self, bodies=None, *, error=None):
        self.bodies = bodies or {b"10": email()}
        self.calls, self.error = [], error
    def login(self, user, password):
        self.calls.append(("login",))
        if self.error: raise RuntimeError(self.error)
        return "OK", []
    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly)); return "OK", [b"1"]
    def response(self, key):
        assert key == "UIDVALIDITY"; return key, [b"99"]
    def uid(self, action, *args):
        self.calls.append((action, *args))
        if action == "search": return "OK", [b" ".join(self.bodies)]
        assert action == "fetch"
        uid, fields = args
        body = self.bodies[uid]
        if fields == "(RFC822.SIZE INTERNALDATE)":
            return "OK", [b'1 (RFC822.SIZE ' + str(len(body)).encode() + b' INTERNALDATE "12-Nov-2026 08:00:00 +0000")']
        assert fields == "(BODY.PEEK[]<0.2000001>)"
        return "OK", [(b"1 (BODY[] {0})", body), b")"]
    def logout(self): self.calls.append(("logout",)); return "BYE", []


def configured(**kwargs):
    return ingest.IMAPConfig(True, "private-owner", "existing@example.invalid", "secret", (SENDER,), **kwargs)


def factory(client):
    def build(host, port, *, ssl_context, timeout):
        assert host == "imap.gmail.com" and port == 993 and timeout == 20
        assert ssl_context.check_hostname
        return client
    return build


def test_imap_is_readonly_uses_transport_identity_and_skips_processed_messages():
    client = FakeIMAP()
    first = ingest.poll_imap(configured(), observed_at=OBSERVED, client_factory=factory(client))
    assert first["status"] == "complete" and first["scanned"] == 1
    message = first["messages"][0]
    assert message["authentication"] == "gmail_dkim_aligned" and message["received_at"] == RECEIVED
    assert message["message_id"] == message["transport_id"]
    assert ("select", "INBOX", True) in client.calls
    assert all(call[0] not in {"store", "expunge", "copy"} for call in client.calls)
    second_client = FakeIMAP()
    second = ingest.poll_imap(configured(), observed_at=OBSERVED,
        existing_ids=(message["transport_id"],), client_factory=factory(second_client))
    assert second["status"] == "complete" and second["scanned"] == 0
    assert not any(call[0] == "fetch" for call in second_client.calls)
    store_key = hashlib.sha256(json.dumps(message["message_id"], ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
    hashed = ingest.poll_imap(configured(), observed_at=OBSERVED,
                             existing_ids=(store_key,), client_factory=factory(FakeIMAP()))
    assert hashed["scanned"] == 0


def test_imap_limits_and_failures_are_explicit_without_private_error_content():
    client = FakeIMAP({b"10": email(), b"11": email()})
    result = ingest.poll_imap(configured(max_messages=1), observed_at=OBSERVED, client_factory=factory(client))
    assert result["scanned"] == result["remaining"] == 1
    rejected = ingest.poll_imap(configured(), observed_at=OBSERVED,
        client_factory=factory(FakeIMAP({b"10": email(auth=False)})))
    assert rejected["status"] == "partial" and not rejected["messages"]
    assert rejected["failures"][0]["code"] == "ValueError"
    assert rejected["failures"][0]["retryable"] is False
    failed = ingest.poll_imap(configured(), observed_at=OBSERVED,
        client_factory=factory(FakeIMAP(error="secret existing@example.invalid")))
    assert failed["status"] == "error" and failed["issues"] == ["imap_unavailable:RuntimeError"]
    assert "secret" not in str(failed) and "existing@example.invalid" not in str(failed)


def test_imap_transport_timeout_is_visible_and_retryable_without_secret_text():
    class TimeoutIMAP(FakeIMAP):
        def uid(self, action, *args):
            if action == "fetch":
                raise TimeoutError("private-secret existing@example.invalid")
            return super().uid(action, *args)
    result = ingest.poll_imap(configured(), observed_at=OBSERVED,
                             client_factory=factory(TimeoutIMAP()))
    assert result["status"] == "partial" and not result["messages"]
    assert result["failures"][0]["retryable"] is True
    assert result["failures"][0]["code"] == "TimeoutError"
    assert "private-secret" not in str(result) and "existing@example.invalid" not in str(result)


def test_poll_does_not_send_existing_credentials_to_a_different_host():
    with pytest.raises(ValueError):
        ingest.poll_imap(replace(configured(), host="evil.invalid"), observed_at=OBSERVED,
                         client_factory=lambda *a, **k: pytest.fail("No connection"))
