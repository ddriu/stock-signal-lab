"""Validation shared by the private, immutable daily-report stores."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import re


def digest_identifier(value: object) -> str:
    identifier = str(value or "")
    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ValueError("El identificador del resumen no es válido.")
    return identifier


def digest_market_date(value: object) -> str:
    text = str(value or "")
    if date.fromisoformat(text).isoformat() != text:
        raise ValueError("La fecha del resumen no es válida.")
    return text


def normalized_digest_report(report: dict[str, object]) -> tuple[str, str, str]:
    if not isinstance(report, dict) or not isinstance(report.get("rows"), list):
        raise ValueError("El informe diario no tiene un formato válido.")
    identifier = digest_identifier(report.get("report_id"))
    market_date = digest_market_date(report.get("as_of"))
    contents = {key: value for key, value in report.items() if key != "report_id"}
    canonical = json.dumps(contents, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != identifier:
        raise ValueError("La versión del informe no coincide con su contenido.")
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    if len(payload.encode("utf-8")) > 2_000_000:
        raise ValueError("El informe diario supera el tamaño permitido.")
    return identifier, market_date, payload


def digest_terminal_state(status: str, error_type: str = "") -> tuple[str, str]:
    if status not in {"sent", "failed", "uncertain"}:
        raise ValueError("El estado del envío no es válido.")
    if not re.fullmatch(r"[A-Za-z0-9_]{0,80}", error_type):
        raise ValueError("Sólo se guarda el tipo de error, nunca credenciales o mensajes SMTP.")
    return status, error_type
