"""Independent, minimal third-party records; never changes the stock model."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import ipaddress
import json
import math
import re
from urllib.parse import urlsplit, urlunsplit

EXTERNAL_ANALYST_VERSION = "external-analysts-v1"
TERMINAL_SOURCE_STATES = {"target_reached", "stopped", "expired", "cancelled", "historical"}
ACTION_LABELS = {"buy": "Comprar", "sell": "Vender", "hold": "Mantener",
                 "watch": "Vigilancia", "unknown": "Sin indicación"}
STATE_LABELS = {
    "pending": "Entrada pendiente", "activated": "Entrada activada", "open": "Posición virtual abierta",
    "target_reached": "Objetivo alcanzado", "stopped": "Stop ejecutado", "expired": "Caducada",
    "cancelled": "Anulada", "not_verifiable": "No verificable", "historical": "Histórica; no ejecutable",
    "ambiguous": "Ambigua: orden intradía desconocido",
}


def stable_id(*parts: object) -> str:
    return sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def utc_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("La fecha necesita hora y zona horaria.") from exc
    else:
        raise ValueError("La fecha necesita hora y zona horaria.")
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("La fecha necesita hora y zona horaria.")
    return stamp.astimezone(timezone.utc).isoformat()


def _text(value: object, maximum: int = 240) -> str:
    if value is None:
        return ""
    result = str(value).strip()
    if len(result) > maximum or any(ord(c) < 32 and c not in "\n\t" for c in result):
        raise ValueError("El campo contiene texto no admitido o es demasiado largo.")
    return result


def public_source_url(value: object, *, provider: str = "") -> str | None:
    if not value:
        return None
    text = _text(value, 2000)
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password
            or parsed.port not in (None, 443) or host in {"localhost", "localhost.localdomain"}
            or "." not in host or host.endswith((".local", ".internal", ".localhost"))):
        raise ValueError("La fuente debe ser un enlace público HTTPS sin credenciales.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("No se admiten direcciones IP como fuente pública.")
    if provider == "perito_inversor" and host not in {"peritoinversor.com", "www.peritoinversor.com"}:
        raise ValueError("El enlace de Perito Inversor no pertenece al proveedor.")
    if provider == "perito_inversor" and (parsed.query or any(
            part in (parsed.path + parsed.fragment).casefold()
            for part in ("unsubscribe", "subscription", "suscripcion", "suscripción", "account", "login", "token="))):
        raise ValueError("No se guardan enlaces de cuenta, seguimiento ni suscripción.")
    # Perito's public instrument identity lives in the fragment; keep it.
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, parsed.fragment))


def _price(value: object) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("Un precio no puede ser un booleano.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("El precio debe ser numérico, no una expresión.") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError("El precio debe ser finito y positivo.")
    result = float(number)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("El precio está fuera del intervalo numérico admitido.")
    return result


def normalize_call(data: dict) -> dict:
    """Validate supplied facts only. Missing evidence remains missing."""
    if not isinstance(data, dict):
        raise ValueError("La recomendación necesita campos estructurados.")
    provider = _text(data.get("provider") or "perito_inversor", 64)
    if not re.fullmatch(r"[a-z0-9_\-]{1,64}", provider):
        raise ValueError("Proveedor no válido.")
    source_id = _text(data.get("source_id"), 512)
    if not source_id:
        raise ValueError("Falta el identificador estable de la recomendación.")
    received = utc_timestamp(data.get("received_at"))
    observed = utc_timestamp(data.get("observed_at"))
    if datetime.fromisoformat(received) > datetime.fromisoformat(observed):
        raise ValueError("La recepción no puede ser posterior a la observación.")
    published = utc_timestamp(data["published_at"]) if data.get("published_at") else None
    expires = utc_timestamp(data["expires_at"]) if data.get("expires_at") else None
    ticker = _text(data.get("ticker"), 32).upper() or None
    if ticker and not re.fullmatch(r"[A-Z0-9.^=\-]{1,32}", ticker):
        raise ValueError("Ticker no válido.")
    raw_currency = _text(data.get("currency"), 8)
    if raw_currency in {"GBp", "GBX"}:
        raise ValueError("La cotización en peniques requiere una conversión explícita; no se trata como GBP.")
    currency = raw_currency.upper() or None
    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("La divisa debe ser explícita y de tres letras.")
    action = data.get("action") or "unknown"
    direction = data.get("direction") or "unknown"
    rule = data.get("entry_rule") or "unknown"
    source_status = data.get("source_status") or "unknown"
    if action not in ACTION_LABELS or direction not in {"long", "short", "neutral", "unknown"}:
        raise ValueError("Tipo de recomendación no reconocido.")
    if rule not in {"limit", "breakout", "market", "unknown"}:
        raise ValueError("Regla de entrada no reconocida.")
    if source_status not in TERMINAL_SOURCE_STATES | {"open", "pending", "unknown"}:
        raise ValueError("Estado de fuente no reconocido.")
    numbers = {name: _price(data.get(name)) for name in ("entry", "stop", "target", "reference_price")}
    evidence = data.get("evidence") or {}
    if isinstance(evidence, list):
        evidence = {"fields": [_text(item, 400) for item in evidence[:15]]}
    if not isinstance(evidence, dict) or len(json.dumps(evidence, ensure_ascii=False, allow_nan=False)) > 12000:
        raise ValueError("La evidencia debe ser breve y estructurada.")
    if any(key.lower() in {"raw", "body", "raw_email", "password", "token", "recipient"} for key in evidence):
        raise ValueError("No se conserva el correo completo ni secretos en la evidencia.")
    issues = []
    if published and datetime.fromisoformat(published) > datetime.fromisoformat(received):
        issues.append("La fecha publicada es posterior a la recepción; verificar la fuente.")
    if evidence.get("published_date"):
        try:
            from datetime import date
            day = date.fromisoformat(evidence["published_date"])
            if day > datetime.fromisoformat(received).date():
                issues.append("La fecha publicada parece posterior a la recepción; verificar la fuente y la zona.")
        except (ValueError, TypeError):
            issues.append("Fecha publicada sin formato verificable.")
    if not ticker:
        issues.append("Ticker sin verificar; no se deduce a partir del nombre.")
    if not currency:
        issues.append("Divisa no documentada.")
    if action != "buy" or direction != "long":
        issues.append("No es una nueva entrada larga; no se abren cortos ni se vende la cartera real.")
    if rule == "unknown":
        issues.append("Regla de activación sin documentar.")
    if not all(numbers.get(key) is not None for key in ("entry", "stop", "target")):
        issues.append("Faltan entrada, stop u objetivo.")
    elif direction == "long" and not numbers["stop"] < numbers["entry"] < numbers["target"]:
        issues.append("Los niveles no forman una operación larga válida.")
    if (numbers["reference_price"] is not None and numbers["stop"] is not None and numbers["target"] is not None
            and not numbers["stop"] < numbers["reference_price"] < numbers["target"]):
        issues.append("El precio de referencia ya está fuera de stop/objetivo; no es una nueva entrada verificable.")
    if source_status in TERMINAL_SOURCE_STATES:
        issues.append("La fuente ya la presenta como histórica o finalizada; se excluye del rendimiento ejecutable.")
    if expires and datetime.fromisoformat(expires) <= datetime.fromisoformat(observed):
        issues.append("La recomendación había caducado cuando se observó.")
    if evidence.get("expires_date") and not expires:
        issues.append("Caducidad sin hora verificable; no se ejecuta hasta confirmarla.")
    url = public_source_url(data.get("source_url"), provider=provider)
    if not url:
        issues.append("Falta un enlace a la fuente original.")
    output = {
        "id": stable_id(provider, source_id), "source_id": source_id, "provider": provider,
        "company_name": _text(data.get("company_name"), 240) or None,
        "ticker": ticker, "exchange": _text(data.get("exchange"), 64) or None,
        "currency": currency, "analyst": _text(data.get("analyst"), 160) or None,
        "published_at": published, "received_at": received, "observed_at": observed,
        "action": action, "direction": direction, "entry_rule": rule,
        "source_status": source_status, "expires_at": expires, "source_url": url,
        **numbers, "evidence": evidence, "executable": not issues, "issues": issues,
        "version": EXTERNAL_ANALYST_VERSION,
    }
    output["state"] = ("historical" if source_status in TERMINAL_SOURCE_STATES else
                       "pending" if output["executable"] else "not_verifiable")
    output.update(reward_risk=None, potential_return_pct=None, risk_pct=None)
    if all(numbers[key] is not None for key in ("entry", "stop", "target")) and numbers["stop"] < numbers["entry"] < numbers["target"]:
        entry, stop, target = (numbers[key] for key in ("entry", "stop", "target"))
        output.update(reward_risk=(target - entry) / (entry - stop),
                      potential_return_pct=(target / entry - 1) * 100,
                      risk_pct=(1 - stop / entry) * 100)
    return output


def revision_id(call: dict) -> str:
    """Repeated receipt changes no forecast; a changed level creates history."""
    normalized = normalize_call(call)
    return stable_id({k: v for k, v in normalized.items()
                      if k not in {"observed_at", "received_at", "state", "issues", "executable",
                                   "potential_return_pct", "risk_pct", "reward_risk"}})


def historical_model_reading(call: dict, snapshots: object) -> dict:
    """Only observations already known when the recommendation was received."""
    from src.analysis_policy import decode_policy_note

    if hasattr(snapshots, "to_dict"):
        snapshots = snapshots.to_dict("records")
    known = datetime.fromisoformat(utc_timestamp(call["received_at"]))
    usable = []
    for row in snapshots or []:
        if row.get("ticker") != call.get("ticker"):
            continue
        try:
            recorded = datetime.fromisoformat(utc_timestamp(row.get("created_at")))
            session = str(row.get("analyzed_at", ""))[:10]
            if recorded > known or session > known.date().isoformat():
                continue
            # Reuse the model's source identity and stored scores, never rescore.
            policy = decode_policy_note(row.get("note"))
            usable.append((recorded, str(row.get("id", "")), row, policy))
        except (ValueError, TypeError):
            continue
    if not usable:
        return {"technical": None, "growth": None, "radar": None, "as_of": None,
                "status": "N/D: no hay observación anterior verificable"}
    _, _, row, policy = max(usable, key=lambda item: (item[0], item[1]))
    def score(value):
        if isinstance(value, bool):
            return None
        try:
            return float(value) if math.isfinite(float(value)) and 0 <= float(value) <= 100 else None
        except (TypeError, ValueError):
            return None
    return {"technical": score(row.get("entry_score")),
            "growth": score((policy or {}).get("primary", {}).get("score")),
            "radar": score(row.get("opportunity_score")), "as_of": str(row.get("analyzed_at"))[:10],
            "status": "Lectura histórica, sin modificar el modelo"}
