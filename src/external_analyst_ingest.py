"""Bounded, read-only ingestion of legitimately received analyst emails.

No network request, attachment extraction, rendering, link visit or persistence
occurs while parsing. Automatic Gmail polling is explicitly opt-in and reuses
the application's existing SMTP identity; it never creates an account.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
import hashlib
import imaplib
import json
import os
from pathlib import Path
import re
import ssl
import unicodedata
from urllib.parse import urlsplit


PARSER_VERSION = "perito-mail-v2"
MAX_MESSAGE_BYTES = 2_000_000
MAX_CALLS = 250
PROVIDER = "perito_inversor"
SOURCE_HOSTS = frozenset({"peritoinversor.com", "www.peritoinversor.com"})


def _fold(value: object) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(value or "").casefold())
                   if not unicodedata.combining(c)).strip()


def _stamp(value: object) -> str:
    """Reject guessed timezones and preserve an explicit UTC availability clock."""
    from src.external_analysts import utc_timestamp
    return utc_timestamp(value)


def _identity(*parts: object) -> str:
    from src.external_analysts import stable_id
    return stable_id(*parts)


def _addresses(values: object) -> tuple[str, ...]:
    if isinstance(values, str):
        values = values.split(",")
    result = []
    for value in values or ():
        address = str(value).strip().casefold()
        if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}", address):
            raise ValueError("Configura remitentes exactos, no dominios ni nombres.")
        result.append(address)
    return tuple(sorted(set(result)))


def _sender(message: EmailMessage, allowed: tuple[str, ...]) -> str:
    headers = message.get_all("From", [])
    parsed = getaddresses(headers)
    if len(headers) != 1 or len(parsed) != 1 or parsed[0][1].casefold() not in allowed:
        raise ValueError("El remitente no coincide con la lista autorizada.")
    return parsed[0][1].casefold()


def _gmail_dkim_aligned(message: EmailMessage, sender: str) -> bool:
    """Only the first Gmail-added result is considered; no arbitrary auth header.

    This check is for messages fetched directly from the configured Gmail IMAP
    mailbox, not uploaded files. Forwarded or unsigned mail fails closed.
    """
    headers = message.get_all("Authentication-Results", [])
    if not headers:
        return False
    header = str(headers[0])
    if not re.match(r"^\s*mx\.google\.com\s*;", header, re.I):
        return False
    domain = sender.rsplit("@", 1)[1]
    for result in header.split(";")[1:]:
        if not re.search(r"\bdkim\s*=\s*pass\b", result, re.I):
            continue
        identity = re.search(r"\bheader\.(?:d|i)\s*=\s*([^\s;]+)", result, re.I)
        if identity:
            signed = identity.group(1).strip('"').casefold().rsplit("@", 1)[-1]
            if signed == domain or domain.endswith("." + signed):
                return True
    return False


_LABELS = {
    "empresa": "company_name", "valor": "company_name", "compania": "company_name",
    "ticker": "ticker", "simbolo": "ticker", "mercado": "exchange", "bolsa": "exchange",
    "divisa": "currency", "moneda": "currency", "analista": "analyst", "autor": "analyst",
    "fecha del pronostico": "published_at", "fecha pronostico": "published_at",
    "fecha original": "published_at", "fecha publicacion": "published_at", "fecha": "published_at",
    "recomendacion": "action", "opinion": "action", "accion": "action",
    "entrada": "entry", "precio de entrada": "entry", "precio entrada": "entry",
    "stop": "stop", "stop loss": "stop", "stop-loss": "stop",
    "objetivo": "target", "precio objetivo": "target", "referencia": "reference_price",
    "precio de referencia": "reference_price", "precio actual": "reference_price",
    "estado": "source_status", "situacion": "source_status", "fuente": "source_url",
    "enlace": "source_url", "ficha": "source_url", "url": "source_url",
    "id": "source_id", "id pronostico": "source_id", "identificador": "source_id",
    "activacion": "entry_rule", "tipo entrada": "entry_rule", "tipo de entrada": "entry_rule",
    "caducidad": "expires_at", "valida hasta": "expires_at", "direccion": "direction",
}


def _label(value: object) -> str | None:
    return _LABELS.get(_fold(value).rstrip(":"))


class _MailHTML(HTMLParser):
    """Extract visible text/explicit tables; scripts and tracking URLs are inert."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.tables, self.row, self.cell = [], [], None, None
        self.ignore = 0
        self.table = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.ignore += 1
        if self.ignore:
            return
        if tag == "table":
            self.table = []
        if tag == "tr":
            self.row = []
        if tag in {"td", "th"}:
            self.cell = []
        if tag == "a":
            href = dict(attrs).get("href", "")
            if _public_url(href):
                text = " " + href + " "
                self.text.append(text)
                if self.cell is not None:
                    self.cell.append(text)
        if tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "dt", "dd"}:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self.ignore = max(0, self.ignore - 1)
            return
        if self.ignore:
            return
        if tag in {"td", "th"} and self.cell is not None:
            if self.row is not None:
                self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        if tag == "tr" and self.row is not None:
            if self.table is not None:
                self.table.append(self.row)
            self.row = None
        if tag == "table" and self.table:
            self.tables.append(self.table)
            self.table = None
        if tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "dt", "dd"}:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.ignore:
            self.text.append(data)
            if self.cell is not None:
                self.cell.append(data)


def _public_url(value: object) -> str | None:
    value = str(value or "").strip()
    if any(character.isspace() or ord(character) < 32 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == "https" and parsed.hostname in SOURCE_HOSTS and not parsed.username
                and not parsed.password and parsed.port in (None, 443) and not parsed.query
                and not re.search(r"(?:unsubscribe|cancelar|account|login|\.netlify)", parsed.path, re.I)
                and len(value) <= 2000):
            return value
    except ValueError:
        pass
    return None


def _amount(value: object) -> float | None:
    text = str(value or "").strip()
    if not text or text in {"—", "-", "N/D"}:
        return None
    # Ranges, percentages and text forecasts are not a single executable level.
    text = re.sub(r"(?:EUR|USD|GBP|CHF|JPY|HKD|CAD|AUD|DKK|SEK|NOK|CNY|kr|€|\$|£)", "", text, flags=re.I).strip()
    if not re.fullmatch(r"\d[\d.,\s]*", text):
        raise ValueError("Nivel de precio ambiguo o no numérico.")
    text = text.replace(" ", "").replace("\u00a0", "")
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        text = text.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif "," in text:
        # Spanish email amounts use comma as decimal separator, not a guessed FX conversion.
        text = text.replace(",", ".")
    result = float(text)
    if not 0 < result < 1e12:
        raise ValueError("Nivel de precio fuera de límites.")
    return result


def _date(value: object, evidence: dict, field_name: str) -> str | None:
    text = str(value or "").strip()
    if not text or text in {"—", "-", "N/D"}:
        return None
    months = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
              "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
    spanish = re.fullmatch(r"(\d{1,2}) de ([a-z]+) de (\d{4})", _fold(text))
    if spanish and spanish.group(2) in months:
        day = datetime(int(spanish.group(3)), months[spanish.group(2)], int(spanish.group(1))).date()
        evidence[field_name.removesuffix("_at") + "_date"] = day.isoformat()
        evidence[field_name.removesuffix("_at") + "_precision"] = "day"
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            day = datetime.strptime(text, fmt).date()
            evidence[field_name.removesuffix("_at") + "_date"] = day.isoformat()
            evidence[field_name.removesuffix("_at") + "_precision"] = "day"
            return None
        except ValueError:
            pass
    try:
        return _stamp(text)
    except (TypeError, ValueError):
        raise ValueError("La fecha no incluye una hora y zona verificables.") from None


def _mapped(value: object, choices: dict, fallback: str) -> str:
    return choices.get(_fold(value), fallback)


def _record(fields: dict, *, received_at: str, observed_at: str, message_id: str, ordinal: int) -> dict:
    from src.external_analysts import normalize_call
    evidence = {"format": "labelled_email", "parser_version": PARSER_VERSION, **fields.get("_evidence", {})}
    company = fields.get("company_name", "").strip()
    ticker = fields.get("ticker", "").strip().upper() or None
    exchange = fields.get("exchange", "").strip() or None
    if ticker and re.fullmatch(r"[A-Z][A-Z0-9 _-]{1,20}:[A-Z0-9.^=-]{1,24}", ticker):
        explicit_exchange, ticker = ticker.split(":", 1)
        if exchange and _fold(exchange) != _fold(explicit_exchange):
            raise ValueError("Mercados explícitos incompatibles.")
        exchange = exchange or explicit_exchange
    published_at = _date(fields.get("published_at"), evidence, "published_at")
    expiry = _date(fields.get("expires_at"), evidence, "expires_at")
    raw_source = fields.get("source_url", "")
    links = re.findall(r"https://[^\s<>\"']+", raw_source)
    source_url = _public_url(links[0]) if len(set(links)) == 1 else _public_url(raw_source)
    if fields.get("source_url") and source_url is None:
        raise ValueError("El enlace de ficha no es público o no pertenece al proveedor.")
    source_id = fields.get("source_id") or source_url
    if not source_id:
        source_id = "mail:" + _identity(message_id, ordinal)
    elif not fields.get("source_id") and source_url:
        # Stable ficha + original issue date identifies a revision, not changed price levels.
        source_id = _identity(source_url, fields.get("analyst", ""), ticker, published_at,
                              evidence.get("published_date"))
    action = _mapped(fields.get("action"), {
        "comprar": "buy", "compra": "buy", "buy": "buy", "vender": "sell", "venta": "sell",
        "sell": "sell", "mantener": "hold", "hold": "hold", "vigilancia": "watch",
        "vigilar": "watch", "watch": "watch", "esperar": "watch",
    }, "unknown")
    direction = _mapped(fields.get("direction"), {
        "alcista": "long", "long": "long", "bajista": "short", "short": "short", "neutral": "neutral",
    }, "long" if action == "buy" else "neutral" if action in {"hold", "watch"} else "unknown")
    source_status = _mapped(fields.get("source_status"), {
        "objetivo alcanzado": "target_reached", "objetivo": "target_reached", "target_reached": "target_reached",
        "stop ejecutado": "stopped", "stop alcanzado": "stopped", "stopped": "stopped", "stop": "stopped",
        "caducada": "expired", "caducado": "expired", "expired": "expired",
        "anulada": "cancelled", "cancelada": "cancelled", "cancelled": "cancelled",
        "abierta": "open", "abierto": "open", "open": "open", "pendiente": "pending", "pending": "pending",
    }, "unknown")
    rule = _mapped(fields.get("entry_rule"), {
        "limite": "limit", "limitada": "limit", "limit": "limit", "ruptura": "breakout",
        "breakout": "breakout", "mercado": "market", "a mercado": "market", "market": "market",
    }, "unknown")
    record = {"provider": PROVIDER, "source_id": str(source_id), "company_name": company,
              "ticker": ticker, "exchange": exchange, "currency": fields.get("currency") or None,
              "analyst": fields.get("analyst") or None, "published_at": published_at,
              "received_at": received_at, "observed_at": observed_at, "expires_at": expiry,
              "action": action, "direction": direction, "entry_rule": rule,
              "source_status": source_status, "source_url": source_url, "evidence": evidence}
    for name in ("entry", "stop", "target", "reference_price"):
        record[name] = _amount(fields.get(name))
    if not company and not ticker:
        raise ValueError("No hay empresa ni ticker identificables.")
    return normalize_call(record)


def _plain_records(text: str) -> list[dict]:
    records, current = [], {}
    for line in text.splitlines():
        # Do not interpret prose, footer instructions, URLs or unlabeled numbers.
        for fragment in re.split(r"\s*[|;]\s*", line):
            match = re.match(r"^\s*([^:]{1,45}):\s*(.*?)\s*$", fragment)
            if not match or not (key := _label(match.group(1))):
                continue
            if key == "company_name" and current.get("company_name"):
                records.append(current)
                current = {}
            if key in current and current[key] != match.group(2):
                # Conflicting labels in one block must not silently overwrite a recommendation.
                current["_conflict"] = True
            current[key] = match.group(2)
    if current:
        records.append(current)
    return records


def _table_records(tables: list) -> list[dict]:
    records = []
    for table in tables:
        if not table:
            continue
        headers = [_label(cell) for cell in table[0]]
        if ("company_name" in headers or "ticker" in headers) and sum(key is not None for key in headers) >= 2:
            for cells in table[1:]:
                if len(cells) != len(headers):
                    continue
                fields = {}
                for key, cell in zip(headers, cells):
                    if key:
                        if key in fields and fields[key] != cell:
                            fields["_conflict"] = True
                        fields[key] = cell
                records.append(fields)
        elif all(len(row) == 2 for row in table):
            fields = {}
            for row in table:
                if key := _label(row[0]):
                    if key in fields and fields[key] != row[1]:
                        fields["_conflict"] = True
                    fields[key] = row[1]
            if fields:
                records.append(fields)
    return records


def _perito_card_records(content: str) -> list[dict] | None:
    """Provider's observed newsletter card schema, not narrative extraction.

    Only td.pi-ficha fields are read. The footer, tracking/unsubscribe links and
    prose are never retained. A directional suggestion is not invented as a buy
    or an executable order; its activation must be documented separately.
    """
    if "pi-ficha" not in content:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise ValueError("Falta beautifulsoup4, incluida con extract-msg, para esta plantilla.") from None
    soup = BeautifulSoup(content, "html.parser")
    for element in soup(["script", "style", "head"]):
        element.decompose()
    records = []
    for card in soup.select("td.pi-ficha")[:MAX_CALLS + 1]:
        paragraphs = card.find_all("p", recursive=False)
        if len(paragraphs) < 2:
            records.append({"_conflict": True})
            continue
        header = " ".join(paragraphs[0].get_text(" ", strip=True).split())
        identity = re.fullmatch(r"([A-Z0-9.^=\-]{1,32})\s*·\s*(.+)", header)
        if not identity:
            records.append({"_conflict": True})
            continue
        fields = {"ticker": identity.group(1), "company_name": identity.group(2),
                  "_evidence": {"format": "perito_newsletter_card_v1",
                                "action_provenance": "directional_suggestion_not_order"}}
        metadata = [part.strip() for part in paragraphs[1].get_text(" ", strip=True).split("·")]
        if len(metadata) >= 4:
            fields["direction"] = _fold(metadata[0]).lstrip("▲▼ ")
            fields["analyst"] = metadata[2]
            fields["published_at"] = metadata[-1]
            if len(metadata) > 4:
                fields["_evidence"]["analyst_organization"] = " · ".join(metadata[3:-1])[:160]
        statuses, activation_levels, activation_rules = set(), set(), set()
        for notice in card.select("td.pi-aviso"):
            for heading in notice.find_all("strong"):
                title = _fold(heading.get_text(" ", strip=True)).rstrip(".")
                status = {"salta el stop loss": "stop ejecutado", "objetivo alcanzado": "objetivo alcanzado",
                          "entrada activada": "abierta", "entrada pendiente": "pendiente",
                          "pendiente de activacion": "pendiente"}.get(title)
                if status:
                    statuses.add(status)
            # This is the provider's explicit activation notice, never a rule
            # inferred from the analyst's free-form opinion or the ticker.
            activation = re.fullmatch(
                r"(?:pendiente de activacion|entrada pendiente)\.?\s*cuenta cuando (baje a|supere)\s+"
                r"((?:eur|usd|gbp|chf|jpy|hkd|cad|aud|dkk|sek|nok|cny|kr|€|\$|£)?\s*\d+(?:[.,]\d+)*)\.?",
                _fold(" ".join(notice.get_text(" ", strip=True).split())),
            )
            if activation:
                activation_rules.add("limit" if activation.group(1) == "baje a" else "breakout")
                try:
                    activation_levels.add(_amount(activation.group(2)))
                except ValueError:
                    fields["_conflict"] = True
                fields["_evidence"]["entry_rule_provenance"] = "source_activation_notice"
        # A completed recommendation can also retain its earlier activation
        # banner. The terminal state takes precedence; competing terminal
        # outcomes (target and stop) still need verification.
        terminal = statuses & {"stop ejecutado", "objetivo alcanzado"}
        if len(terminal) > 1 or (not terminal and len(statuses) > 1):
            fields["_conflict"] = True
        elif terminal or statuses:
            fields["source_status"] = next(iter(terminal or statuses))
        currency_labels = set()
        for table in card.find_all("table"):
            if table.find("table"):
                continue
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"], recursive=False)
                if len(cells) != 2:
                    continue
                key = _label(cells[0].get_text(" ", strip=True))
                if key not in {"entry", "stop", "target", "reference_price", "currency", "entry_rule", "action"}:
                    continue
                value = cells[1].get_text(" ", strip=True)
                if key == "reference_price":
                    value = re.sub(r"\s*\([^()]*%[^()]*\)\s*$", "", value)
                if key in fields and fields[key] != value:
                    fields["_conflict"] = True
                fields[key] = value
                if key in {"entry", "stop", "target", "reference_price"}:
                    currency_labels.update(re.findall(r"EUR|USD|GBP|CHF|JPY|HKD|CAD|AUD|DKK|SEK|NOK|CNY|kr|€|\$|£", value))
        if activation_levels:
            try:
                entry = _amount(fields.get("entry"))
                explicit_rule = _mapped(fields.get("entry_rule"), {
                    "limite": "limit", "limitada": "limit", "limit": "limit", "ruptura": "breakout",
                    "breakout": "breakout", "mercado": "market", "a mercado": "market", "market": "market",
                }, "unknown")
                if (len(activation_levels) != 1 or entry not in activation_levels or len(activation_rules) != 1
                        or (fields.get("entry_rule") and explicit_rule not in activation_rules)):
                    fields["_conflict"] = True
                else:
                    fields["entry_rule"] = next(iter(activation_rules))
                    fields["_evidence"]["activation_level"] = entry
            except ValueError:
                fields["_conflict"] = True
        iso = currency_labels & {"EUR", "USD", "GBP", "CHF", "JPY", "HKD", "CAD", "AUD", "DKK", "SEK", "NOK", "CNY"}
        if len(iso) == 1:
            fields["currency"] = next(iter(iso))
        elif currency_labels == {"€"}:
            fields["currency"] = "EUR"
        elif len(iso) > 1:
            fields["_conflict"] = True
        if currency_labels and not fields.get("currency"):
            fields["_evidence"]["ambiguous_currency_marker"] = ",".join(sorted(currency_labels))[:30]
        sources = {link.get("href") for link in card.find_all("a", href=True)
                   if _public_url(link.get("href"))}
        if len(sources) == 1:
            fields["source_url"] = next(iter(sources))
            fragment = urlsplit(fields["source_url"]).fragment
            if re.fullmatch(r"ins-[A-Za-z0-9_\-]{1,100}", fragment):
                fields["source_id"] = fragment
        elif len(sources) > 1:
            fields["_conflict"] = True
        records.append(fields)
    return records


def parse_email_bytes(raw: bytes, *, received_at: object, observed_at: object,
                      allowed_senders: object, require_gmail_auth: bool = False) -> dict:
    """Parse only explicit recommendations, never the number advertised in a subject."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("El mensaje está vacío o supera el límite de tamaño.")
    received, observed = _stamp(received_at), _stamp(observed_at)
    if datetime.fromisoformat(observed) < datetime.fromisoformat(received):
        raise ValueError("La recepción no puede ser posterior a la observación.")
    allowed = _addresses(allowed_senders)
    if not allowed:
        raise ValueError("Falta la lista exacta de remitentes autorizados.")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if message.defects:
        raise ValueError("La estructura del mensaje no es verificable.")
    sender = _sender(message, allowed)
    auth = _gmail_dkim_aligned(message, sender)
    if require_gmail_auth and not auth:
        raise ValueError("El correo no tiene una firma DKIM alineada verificada por Gmail.")
    message_id = str(message.get("Message-ID") or "").strip()
    if len(message.get_all("Message-ID", [])) > 1:
        raise ValueError("El mensaje contiene identificadores incompatibles.")
    message_id = _identity("message", message_id or hashlib.sha256(raw).hexdigest())
    subject = str(message.get("Subject") or "")
    advertised = re.search(r"\b(\d{1,5})\s+(?:nuevas?\s+)?(?:sugerencias?|recomendaciones?|pron[oó]sticos?)\b", subject, re.I)
    announced_count = int(advertised.group(1)) if advertised else None
    body = message.get_body(preferencelist=("html", "plain"))
    if body is None:
        body = message if message.get_content_type() in {"text/plain", "text/html"} else None
    tables, issues, fields = [], [], []
    if body is not None and body.get_content_disposition() != "attachment":
        content = body.get_content()
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="strict")
        if body.get_content_type() == "text/html":
            cards = _perito_card_records(content)
            if cards is not None:
                fields = cards
            else:
                parsed = _MailHTML()
                parsed.feed(content)
                tables = _table_records(parsed.tables)
                text = "".join(parsed.text)
                fields = tables or _plain_records(text)
        else:
            fields = _plain_records(content)
    calls, seen = [], set()
    for ordinal, fields_row in enumerate(fields[:MAX_CALLS]):
        if fields_row.get("_conflict"):
            issues.append(f"recommendation_{ordinal + 1}: conflicting_labels")
            continue
        try:
            call = _record(fields_row, received_at=received, observed_at=observed,
                           message_id=message_id, ordinal=ordinal)
        except (ValueError, TypeError, OverflowError):
            issues.append(f"recommendation_{ordinal + 1}: invalid_or_ambiguous_fields")
            continue
        identity = call.get("id") or call.get("source_id")
        if identity in seen:
            issues.append(f"recommendation_{ordinal + 1}: duplicate_in_message")
            continue
        seen.add(identity)
        calls.append(call)
    if len(fields) > MAX_CALLS:
        issues.append("recommendation_limit_exceeded")
    if not fields:
        issues.append("no_supported_labelled_recommendations")
    if announced_count is not None and announced_count != len(calls):
        issues.append("announced_count_differs_from_extracted")
    return {"message_id": message_id, "provider": PROVIDER, "received_at": received,
            "observed_at": observed, "announced_count": announced_count,
            "extracted_count": len(calls), "calls": calls, "issues": issues,
            "parser_version": PARSER_VERSION, "body_hash": hashlib.sha256(raw).hexdigest(),
            "sender_validated": True,
            "authentication": "gmail_dkim_aligned" if require_gmail_auth else "manual_unverified"}


def parse_msg_bytes(raw: bytes, **kwargs) -> dict:
    """Read an Outlook MSG in memory; never save or traverse its attachments."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("El mensaje está vacío o supera el límite de tamaño.")
    try:
        import extract_msg
    except ImportError:
        raise ValueError("Instala la dependencia opcional extract-msg==0.58.2 para importar .msg.") from None
    try:
        with extract_msg.openMsg(raw, delayAttachments=True) as item:
            original = item.header
            message = EmailMessage()
            for name in ("From", "Subject", "Message-ID", "Date"):
                value = original.get(name) if hasattr(original, "get") else None
                if value:
                    # extract-msg's legacy Header object retains RFC folding.
                    # Unfold continuations only; bare newlines still fail closed.
                    message[name] = re.sub(r"\r?\n[ \t]+", " ", str(value))
            if not message.get("From") and item.sender:
                message["From"] = str(item.sender)
            if not message.get("Subject") and item.subject:
                message["Subject"] = str(item.subject)
            if not message.get("Message-ID"):
                message["Message-ID"] = "<ssl-" + hashlib.sha256(raw).hexdigest() + "@local.invalid>"
            message.set_content(item.body or "")
            if item.htmlBody:
                html = item.htmlBody
                if isinstance(html, bytes):
                    html = html.decode("utf-8", errors="strict")
                message.add_alternative(html, subtype="html")
            result = parse_email_bytes(message.as_bytes(), **kwargs)
            result["body_hash"] = hashlib.sha256(raw).hexdigest()
            return result
    except (ValueError, TypeError):
        raise
    except Exception:
        raise ValueError("El archivo .msg no contiene un mensaje legible.") from None


def parse_mail_file(path: str | Path, **kwargs) -> dict:
    file = Path(path)
    if file.stat().st_size > MAX_MESSAGE_BYTES:
        raise ValueError("El mensaje supera el límite de tamaño.")
    raw = file.read_bytes()
    if file.suffix.casefold() == ".msg":
        return parse_msg_bytes(raw, **kwargs)
    if file.suffix.casefold() == ".eml":
        return parse_email_bytes(raw, **kwargs)
    raise ValueError("Selecciona un correo .eml o .msg.")


@dataclass(frozen=True)
class IMAPConfig:
    enabled: bool = False
    owner: str = ""
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    allowed_senders: tuple[str, ...] = ()
    host: str = "imap.gmail.com"
    folder: str = "INBOX"
    max_messages: int = 20
    lookback_days: int = 30

    @classmethod
    def from_existing_mail(cls, email_config=None, *, env=None):
        from src.email_sender import load_email_config
        values = os.environ if env is None else env
        enabled = values.get("EXTERNAL_ANALYSTS_IMAP_ENABLED", "false").strip().casefold() in {"1", "true", "yes"}
        if not enabled:
            return cls()
        configured = email_config or load_email_config()
        if configured.host.casefold() != "smtp.gmail.com" or not configured.configured:
            raise ValueError("La recepción necesita la cuenta Gmail ya configurada en Stock Signal Lab.")
        owner = values.get("EXTERNAL_ANALYSTS_MAILBOX_OWNER", "").strip()
        senders = _addresses(values.get("PERITO_ALLOWED_SENDERS", ""))
        folder = (values.get("EXTERNAL_ANALYSTS_IMAP_FOLDER") or "INBOX").strip() or "INBOX"
        if not owner or not senders or not re.fullmatch(r"[A-Za-z0-9 _/\-\[\]]{1,100}", folder):
            raise ValueError("Faltan propietario, remitentes exactos o carpeta IMAP válidos.")
        return cls(True, owner, configured.username, configured.password, senders, folder=folder)


def _ok(response, operation: str):
    status, data = response
    if status != "OK":
        raise ValueError(f"IMAP no confirmó {operation}.")
    return data


def poll_imap(config: IMAPConfig, *, observed_at: object, existing_ids=(), client_factory=None) -> dict:
    """Read selected senders with EXAMINE/BODY.PEEK and return structured results.

    Callers persist successful and terminal failed transport IDs separately to
    avoid duplicates/poison-message starvation. Transient transport failures
    stay retryable, never skipped forever. No message is marked read or removed.
    """
    result = {"status": "disabled", "messages": [], "failures": [], "issues": [],
              "scanned": 0, "remaining": 0, "uidvalidity": None}
    if not config.enabled:
        return result
    observed = _stamp(observed_at)
    clock = datetime.fromisoformat(observed)
    if (config.host != "imap.gmail.com" or not config.owner or not config.username or not config.password
            or not 1 <= config.max_messages <= 50 or not 1 <= config.lookback_days <= 90
            or not re.fullmatch(r"[A-Za-z0-9 _/\-\[\]]{1,100}", config.folder)
            or not _addresses(config.allowed_senders)):
        raise ValueError("La configuración de recepción no es válida.")
    seen = set(existing_ids)
    client_factory = client_factory or imaplib.IMAP4_SSL
    client = None
    try:
        client = client_factory(config.host, 993, ssl_context=ssl.create_default_context(), timeout=20)
        _ok(client.login(config.username, config.password), "la autenticación")
        _ok(client.select(config.folder, readonly=True), "la carpeta de solo lectura")
        validity_response = client.response("UIDVALIDITY")
        validity_values = validity_response[1] if len(validity_response) == 2 else None
        validity = (validity_values or [b""])[0]
        validity = validity.decode("ascii") if isinstance(validity, bytes) else str(validity)
        if not validity.isdigit():
            raise ValueError("Falta UIDVALIDITY verificable.")
        result["uidvalidity"] = validity
        matched = set()
        since = (clock - timedelta(days=config.lookback_days)).strftime("%d-%b-%Y")
        for sender in config.allowed_senders:
            data = _ok(client.uid("search", None, "SINCE", since, "FROM", '"' + sender + '"'), "la búsqueda")
            for value in data or []:
                if isinstance(value, bytes):
                    matched.update(uid for uid in value.split() if uid.isdigit())
        pending = []
        account = _identity(config.username.casefold(), config.folder)
        for uid in sorted(matched, key=int):
            key = _identity("imap", account, validity, uid.decode("ascii"))
            # The store deliberately saves hashed keys, not transport metadata.
            store_key = hashlib.sha256(json.dumps(key, ensure_ascii=False, sort_keys=True,
                                                  separators=(",", ":")).encode("utf-8")).hexdigest()
            if key not in seen and store_key not in seen:
                pending.append((uid, key))
        result["remaining"] = max(0, len(pending) - config.max_messages)
        for uid, transport_id in pending[:config.max_messages]:
            result["scanned"] += 1
            try:
                meta = _ok(client.uid("fetch", uid, "(RFC822.SIZE INTERNALDATE)"), "los metadatos")
                metadata = b" ".join(item[0] if isinstance(item, tuple) else item
                                     for item in meta or [] if isinstance(item, (bytes, tuple)))
                size = re.search(rb"RFC822\.SIZE\s+(\d+)", metadata)
                date = re.search(rb'INTERNALDATE\s+"([^"\r\n]+)"', metadata)
                if not size or not date or not 0 < int(size.group(1)) <= MAX_MESSAGE_BYTES:
                    raise ValueError("Los metadatos o el tamaño del correo no son válidos.")
                date_text = date.group(1).decode("ascii").replace("-", " ", 2)
                received = parsedate_to_datetime(date_text)
                body = _ok(client.uid("fetch", uid, "(BODY.PEEK[]<0.2000001>)"), "el cuerpo de solo lectura")
                chunks = [item[1] for item in body or [] if isinstance(item, tuple) and isinstance(item[1], bytes)]
                if len(chunks) != 1 or len(chunks[0]) != int(size.group(1)):
                    raise ValueError("El cuerpo recibido está incompleto.")
                parsed = parse_email_bytes(chunks[0], received_at=received, observed_at=observed,
                                           allowed_senders=config.allowed_senders, require_gmail_auth=True)
                parsed["rfc_message_id"] = parsed["message_id"]
                parsed["message_id"] = parsed["transport_id"] = transport_id
                result["messages"].append(parsed)
            except Exception as error:
                retryable = isinstance(error, (OSError, TimeoutError, imaplib.IMAP4.abort))
                result["failures"].append({"message_id": transport_id, "code": type(error).__name__,
                                           "retryable": retryable})
        result["status"] = "partial" if result["failures"] else "complete"
    except Exception as error:
        result["status"] = "error"
        result["issues"].append("imap_unavailable:" + type(error).__name__)
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                result["issues"].append("imap_logout_unconfirmed")
    return result
