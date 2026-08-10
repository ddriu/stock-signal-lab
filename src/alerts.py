"""Preferencias, evaluación y contenido de las alertas por correo.

El módulo no envía órdenes ni convierte una señal técnica en una recomendación
absoluta. Sólo decide si un cambio de estado merece aparecer en el resumen de
un usuario.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr
import html
from typing import Iterable

from src.signal_engine import (
    LABEL_BUY,
    LABEL_REDUCE,
    LABEL_SELL,
    LABEL_STRONG,
    SignalResult,
)


ALERT_PREFERENCE_COLUMNS = [
    "owner",
    "email",
    "enabled",
    "alert_buy",
    "alert_reduce",
    "alert_sell",
    "include_group",
    "minimum_buy_score",
    "only_changes",
    "updated_at",
]

ALERT_STATE_COLUMNS = [
    "owner",
    "ticker",
    "signature",
    "entry_score",
    "entry_label",
    "position_label",
    "price",
    "evaluated_at",
    "notified_at",
]


@dataclass(frozen=True)
class AlertPreferences:
    """Configuración privada de correo para una cuenta de la aplicación."""

    owner: str
    email: str = ""
    enabled: bool = False
    alert_buy: bool = True
    alert_reduce: bool = True
    alert_sell: bool = True
    include_group: bool = True
    minimum_buy_score: int = 65
    only_changes: bool = True
    updated_at: str = ""


@dataclass(frozen=True)
class AlertCandidate:
    """Aviso explicable producido por una señal técnica."""

    ticker: str
    kind: str
    title: str
    entry_score: int
    entry_label: str
    position_label: str
    price: float
    as_of: str
    explanation: str
    signature: str
    held: bool
    company_name: str = ""


@dataclass(frozen=True)
class AlertState:
    """Último estado evaluado, utilizado para no repetir avisos idénticos."""

    owner: str
    ticker: str
    signature: str
    entry_score: int
    entry_label: str
    position_label: str
    price: float
    evaluated_at: str
    notified_at: str | None = None


def is_valid_email(value: str) -> bool:
    """Validación deliberadamente sencilla para direcciones de notificación."""

    name, address = parseaddr(value.strip())
    del name
    if address != value.strip() or address.count("@") != 1:
        return False
    local, domain = address.rsplit("@", 1)
    return bool(local and "." in domain and not domain.startswith("."))


def normalize_alert_preferences(
    *,
    owner: str,
    email: str = "",
    enabled: bool = False,
    alert_buy: bool = True,
    alert_reduce: bool = True,
    alert_sell: bool = True,
    include_group: bool = True,
    minimum_buy_score: int = 65,
    only_changes: bool = True,
    updated_at: str | None = None,
) -> AlertPreferences:
    normalized_owner = owner.strip().lower()
    normalized_email = email.strip().lower()
    threshold = int(minimum_buy_score)
    if not normalized_owner:
        raise ValueError("Las alertas necesitan un usuario propietario.")
    if normalized_email and not is_valid_email(normalized_email):
        raise ValueError("Introduce una dirección de correo válida.")
    if enabled and not normalized_email:
        raise ValueError("Introduce un correo antes de activar los avisos.")
    if not 55 <= threshold <= 100:
        raise ValueError("El umbral de compra debe estar entre 55 y 100.")
    if enabled and not any((alert_buy, alert_reduce, alert_sell)):
        raise ValueError("Activa al menos un tipo de aviso.")
    return AlertPreferences(
        owner=normalized_owner,
        email=normalized_email,
        enabled=bool(enabled),
        alert_buy=bool(alert_buy),
        alert_reduce=bool(alert_reduce),
        alert_sell=bool(alert_sell),
        include_group=bool(include_group),
        minimum_buy_score=threshold,
        only_changes=bool(only_changes),
        updated_at=updated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
    )


def preferences_from_mapping(
    values: dict[str, object] | None,
    *,
    owner: str,
) -> AlertPreferences:
    """Convierte una fila SQLite/PostgREST en preferencias tipadas."""

    row = values or {}
    return normalize_alert_preferences(
        owner=str(row.get("owner") or owner),
        email=str(row.get("email") or ""),
        enabled=bool(row.get("enabled", False)),
        alert_buy=bool(row.get("alert_buy", True)),
        alert_reduce=bool(row.get("alert_reduce", True)),
        alert_sell=bool(row.get("alert_sell", True)),
        include_group=bool(row.get("include_group", True)),
        minimum_buy_score=int(row.get("minimum_buy_score", 65)),
        only_changes=bool(row.get("only_changes", True)),
        updated_at=str(row.get("updated_at") or ""),
    )


def signal_signature(signal: SignalResult, *, held: bool) -> str:
    """Categoría estable; pequeñas variaciones de score no generan correo repetido."""

    if held and signal.position_label in {LABEL_REDUCE, LABEL_SELL}:
        return f"position:{signal.position_label}"
    if not held and signal.label in {LABEL_BUY, LABEL_STRONG}:
        return f"entry:{signal.label}"
    return "neutral"


def build_alert_candidate(
    signal: SignalResult,
    *,
    price: float,
    held: bool,
    preferences: AlertPreferences,
    company_name: str = "",
) -> AlertCandidate | None:
    """Aplica las preferencias a una señal ya calculada."""

    signature = signal_signature(signal, held=held)
    kind = ""
    title = ""
    if (
        held
        and signal.position_label == LABEL_SELL
        and preferences.alert_sell
    ):
        kind = "Venta"
        title = "Revisar posible salida"
    elif (
        held
        and signal.position_label == LABEL_REDUCE
        and preferences.alert_reduce
    ):
        kind = "Reducción"
        title = "Revisar el riesgo de la posición"
    elif (
        not held
        and signal.label in {LABEL_BUY, LABEL_STRONG}
        and signal.score >= preferences.minimum_buy_score
        and preferences.alert_buy
    ):
        kind = "Compra"
        title = (
            "Momento técnico fuerte"
            if signal.label == LABEL_STRONG
            else "Entrada técnica interesante"
        )
    if not kind:
        return None
    return AlertCandidate(
        ticker=signal.ticker,
        kind=kind,
        title=title,
        entry_score=signal.score,
        entry_label=signal.label,
        position_label=signal.position_label,
        price=float(price),
        as_of=signal.as_of.date().isoformat(),
        explanation=signal.explanation,
        signature=signature,
        held=held,
        company_name=company_name.strip(),
    )


def build_alert_state(
    *,
    owner: str,
    signal: SignalResult,
    price: float,
    held: bool,
    notified: bool,
    evaluated_at: str | None = None,
) -> AlertState:
    now = evaluated_at or datetime.now().astimezone().isoformat(timespec="seconds")
    return AlertState(
        owner=owner.strip().lower(),
        ticker=signal.ticker.strip().upper(),
        signature=signal_signature(signal, held=held),
        entry_score=int(signal.score),
        entry_label=signal.label,
        position_label=signal.position_label,
        price=float(price),
        evaluated_at=now,
        notified_at=now if notified else None,
    )


def filter_changed_candidates(
    candidates: Iterable[AlertCandidate],
    previous_signatures: dict[str, str],
    *,
    only_changes: bool,
) -> list[AlertCandidate]:
    if not only_changes:
        return list(candidates)
    return [
        candidate
        for candidate in candidates
        if previous_signatures.get(candidate.ticker) != candidate.signature
    ]


def build_digest_content(
    display_name: str,
    candidates: Iterable[AlertCandidate],
) -> tuple[str, str, str]:
    """Devuelve asunto, versión de texto y HTML del resumen."""

    rows = list(candidates)
    alert_word = f"{len(rows)} alerta{'s' if len(rows) != 1 else ''}"

    def display_label(candidate: AlertCandidate) -> str:
        ticker = candidate.ticker.strip().upper()
        name = candidate.company_name.strip()
        if not name or name.casefold() == ticker.casefold():
            return ticker
        return f"{name} ({ticker})"

    # El asunto muestra una empresa para que la alerta sea reconocible incluso
    # antes de abrir el mensaje. El cuerpo conserva todas las empresas.
    subject = f"Stock Signal Lab · {alert_word}"
    if rows:
        subject += f" · {display_label(rows[0])}"
        if len(rows) > 1:
            subject += f" y {len(rows) - 1} más"
    plain_lines = [
        f"Hola {display_name},",
        "",
        "Estas señales han cambiado y merecen revisión:",
        "",
    ]
    html_rows: list[str] = []
    color_by_kind = {
        "Compra": "#16835b",
        "Reducción": "#b7791f",
        "Venta": "#c53030",
    }
    for candidate in rows:
        label = display_label(candidate)
        plain_lines.extend(
            [
                f"{label} · {candidate.kind} · {candidate.title}",
                (
                    f"Entrada {candidate.entry_score}/100 ({candidate.entry_label}); "
                    f"posición: {candidate.position_label}; cierre: {candidate.price:.2f}."
                ),
                candidate.explanation,
                "",
            ]
        )
        color = color_by_kind.get(candidate.kind, "#2563eb")
        html_rows.append(
            f"""
            <div style="border:1px solid #e2e8f0;border-left:5px solid {color};
                        border-radius:10px;padding:14px 16px;margin:12px 0">
              <div style="font-size:12px;color:{color};font-weight:700">
                {html.escape(candidate.kind.upper())}
              </div>
              <h3 style="margin:4px 0 8px;color:#14213d">
                {html.escape(label)} · {html.escape(candidate.title)}
              </h3>
              <p style="margin:0 0 8px">
                Entrada <strong>{candidate.entry_score}/100</strong>
                ({html.escape(candidate.entry_label)}) · Posición:
                <strong>{html.escape(candidate.position_label)}</strong> ·
                cierre {candidate.price:.2f} · datos {html.escape(candidate.as_of)}
              </p>
              <p style="margin:0;color:#475569">{html.escape(candidate.explanation)}</p>
            </div>
            """
        )
    disclaimer = (
        "Son señales probabilísticas basadas en datos históricos. No constituyen "
        "asesoramiento financiero ni garantizan rentabilidad."
    )
    plain_lines.extend([disclaimer, "Puedes desactivar estos avisos desde la aplicación."])
    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:680px;margin:auto;color:#1e293b">
      <h2 style="color:#14213d">Stock Signal Lab</h2>
      <p>Hola {html.escape(display_name)},</p>
      <p>Estas señales han cambiado y merecen revisión:</p>
      {''.join(html_rows)}
      <p style="font-size:12px;color:#64748b;margin-top:22px">{html.escape(disclaimer)}</p>
      <p style="font-size:12px;color:#64748b">
        Puedes cambiar o desactivar los avisos desde la aplicación.
      </p>
    </div>
    """
    return subject, "\n".join(plain_lines), html_body
