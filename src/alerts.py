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
from src.entry_opportunity import non_linking_ticker_text


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
    timing_score: int | None = None
    opportunity_score: int | None = None
    opportunity_status: str = ""
    preferred_entry: str = ""
    event_label: str = ""
    fundamental_filter_score: int | None = None
    fundamental_filter_label: str = ""


@dataclass(frozen=True)
class DailyOverviewRow:
    """Lecturas consolidadas de una empresa para el segundo correo diario."""

    ticker: str
    company_name: str
    held: bool
    price: float
    as_of: str
    technical_score: int | None
    technical_label: str
    position_label: str
    growth_score: int | None = None
    growth_label: str = ""
    fundamental_score: int | None = None
    fundamental_label: str = ""
    opportunity_score: int | None = None
    opportunity_status: str = ""
    changed: bool = False
    change_kind: str = ""
    previous_state: str = ""
    current_state: str = ""
    data_note: str = ""


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


def _signature_state(signature: str, *, held: bool) -> tuple[str, tuple[int, int]]:
    """Traduce el estado persistido a una lectura humana y comparable."""

    normalized = str(signature or "").strip()
    if not normalized or normalized == "neutral":
        label = "Mantener / vigilar" if held else "Vigilancia"
        return label, (2 if held else 0, 0)

    if normalized.startswith("position:"):
        position = normalized.split(":", 1)[1]
        ranks = {LABEL_SELL: 0, LABEL_REDUCE: 1}
        return position, (ranks.get(position, 1), 0)

    if normalized.startswith("entry:"):
        parts = normalized.split(":")
        technical = parts[1] if len(parts) > 1 else "Entrada"
        status = parts[2] if len(parts) > 2 else ""
        technical_rank = 2 if technical == LABEL_STRONG else 1
        status_states = {
            "COMPRABLE": ("Entrada validada", 4),
            "ESPERAR_PRECIO": ("Esperar mejor precio", 3),
            "EXTENDIDA": ("No perseguir: extendida", 2),
            "EVENTO_NO_ENTRAR": ("Esperar evento", 1),
            "OPORTUNIDAD_INCOMPLETA": ("Entrada sin validar", 1),
        }
        if status in status_states:
            label, rank = status_states[status]
            return label, (rank, technical_rank)
        return technical, (3, technical_rank)

    return "Estado actualizado", (1, 0)


def describe_state_change(
    previous_signature: str | None,
    current_signature: str,
    *,
    held: bool,
) -> tuple[bool, str, str, str]:
    """Distingue primera lectura, mejora y deterioro sin llamar cambio a todo."""

    current_label, current_rank = _signature_state(current_signature, held=held)
    if previous_signature is None:
        return False, "new", "", current_label

    previous_label, previous_rank = _signature_state(previous_signature, held=held)
    if previous_signature == current_signature:
        return False, "", previous_label, current_label
    role_transition = (held and previous_signature.startswith("entry:")) or (
        not held and previous_signature.startswith("position:")
    )
    if role_transition:
        kind = "change"
    elif current_rank > previous_rank:
        kind = "improvement"
    elif current_rank < previous_rank:
        kind = "deterioration"
    else:
        kind = "change"
    return True, kind, previous_label, current_label


def build_alert_candidate(
    signal: SignalResult,
    *,
    price: float,
    held: bool,
    preferences: AlertPreferences,
    company_name: str = "",
    timing_score: int | None = None,
    opportunity_score: int | None = None,
    opportunity_status: str = "",
    preferred_entry: str = "",
    event_label: str = "",
    fundamental_filter_score: int | None = None,
    fundamental_filter_label: str = "",
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
        timing_score=timing_score,
        opportunity_score=opportunity_score,
        opportunity_status=opportunity_status.strip(),
        preferred_entry=preferred_entry.strip(),
        event_label=event_label.strip(),
        fundamental_filter_score=fundamental_filter_score,
        fundamental_filter_label=fundamental_filter_label.strip(),
    )


def build_alert_state(
    *,
    owner: str,
    signal: SignalResult,
    price: float,
    held: bool,
    notified: bool,
    evaluated_at: str | None = None,
    signature: str | None = None,
    previous_notified_at: str | None = None,
) -> AlertState:
    now = evaluated_at or datetime.now().astimezone().isoformat(timespec="seconds")
    return AlertState(
        owner=owner.strip().lower(),
        ticker=signal.ticker.strip().upper(),
        signature=signature or signal_signature(signal, held=held),
        entry_score=int(signal.score),
        entry_label=signal.label,
        position_label=signal.position_label,
        price=float(price),
        evaluated_at=now,
        # Conserva la última entrega real. De lo contrario, una revisión sin
        # novedades borraría al día siguiente la única evidencia del correo.
        notified_at=now if notified else previous_notified_at,
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
        raw_ticker = candidate.ticker.strip().upper()
        ticker = non_linking_ticker_text(raw_ticker)
        name = candidate.company_name.strip()
        if not name or name.casefold() == raw_ticker.casefold():
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
        "TOP OPORTUNIDADES",
    ]
    opportunity_rows = sorted(
        (
            candidate
            for candidate in rows
            if candidate.kind == "Compra" and candidate.opportunity_score is not None
        ),
        key=lambda candidate: int(candidate.opportunity_score or 0),
        reverse=True,
    )[:5]
    for candidate in opportunity_rows:
        timing_text = (
            f" / Timing {candidate.timing_score}"
            if candidate.timing_score is not None
            else ""
        )
        zone_text = (
            f" · Zona {candidate.preferred_entry}"
            if candidate.preferred_entry
            else ""
        )
        plain_lines.append(
            f"{display_label(candidate)} · Oportunidad {candidate.opportunity_score}"
            f"{timing_text} · {candidate.opportunity_status or candidate.title}{zone_text}"
        )
    if not opportunity_rows:
        plain_lines.append("Sin nuevas entradas con score conjunto disponible.")
    plain_lines.extend(
        [
            "",
            "Estas señales han cambiado y merecen revisión:",
            "",
        ]
    )
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
                    f"Score técnico {candidate.entry_score}/100 ({candidate.entry_label}); "
                    f"posición: {candidate.position_label}; cierre: {candidate.price:.2f}."
                ),
                (
                    f"Filtro fundamental {candidate.fundamental_filter_score}/100 "
                    f"({candidate.fundamental_filter_label})."
                    if candidate.fundamental_filter_score is not None
                    else (
                        "Filtro fundamental: datos insuficientes."
                        if candidate.kind == "Compra"
                        else ""
                    )
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
                Score técnico <strong>{candidate.entry_score}/100</strong>
                ({html.escape(candidate.entry_label)}) · Posición:
                <strong>{html.escape(candidate.position_label)}</strong> ·
                cierre {candidate.price:.2f} · datos {html.escape(candidate.as_of)}
              </p>
              {
                  '<p style="margin:0 0 8px">Timing <strong>'
                  + str(candidate.timing_score)
                  + '/100</strong> · Oportunidad <strong>'
                  + str(candidate.opportunity_score)
                  + '/100</strong> · '
                  + html.escape(candidate.opportunity_status)
                  + (f' · Zona {html.escape(candidate.preferred_entry)}' if candidate.preferred_entry else '')
                  + '</p>'
                  if candidate.opportunity_score is not None
                  else ''
              }
              {
                  '<p style="margin:0 0 8px">Calidad fundamental <strong>'
                  + str(candidate.fundamental_filter_score)
                  + '/100</strong> · '
                  + html.escape(candidate.fundamental_filter_label)
                  + '</p>'
                  if candidate.fundamental_filter_score is not None
                  else (
                      '<p style="margin:0 0 8px;color:#64748b">Calidad fundamental: datos insuficientes</p>'
                      if candidate.kind == 'Compra'
                      else ''
                  )
              }
              <p style="margin:0;color:#475569">{html.escape(candidate.explanation)}</p>
            </div>
            """
        )
    disclaimer = (
        "Son señales probabilísticas basadas en datos históricos. No constituyen "
        "asesoramiento financiero ni garantizan rentabilidad."
    )
    plain_lines.extend([disclaimer, "Puedes desactivar estos avisos desde la aplicación."])
    html_opportunity_rows = "".join(
        '<p style="margin:8px 0">'
        + html.escape(display_label(candidate))
        + " · Oportunidad <strong>"
        + str(candidate.opportunity_score)
        + "/100</strong>"
        + (
            " · Timing " + str(candidate.timing_score) + "/100"
            if candidate.timing_score is not None
            else ""
        )
        + '<br><span style="color:#475569">'
        + html.escape(candidate.opportunity_status or candidate.title)
        + (
            " · Zona " + html.escape(candidate.preferred_entry)
            if candidate.preferred_entry
            else ""
        )
        + "</span></p>"
        for candidate in opportunity_rows
    )
    if not html_opportunity_rows:
        html_opportunity_rows = (
            '<p style="margin:8px 0;color:#64748b">'
            "Sin nuevas entradas con score conjunto disponible.</p>"
        )
    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:680px;margin:auto;color:#1e293b">
      <h2 style="color:#14213d">Stock Signal Lab</h2>
      <p>Hola {html.escape(display_name)},</p>
      <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;
                  padding:12px 16px;margin:12px 0">
        <strong>🔥 TOP OPORTUNIDADES</strong>
        {html_opportunity_rows}
      </div>
      <p>Estas señales han cambiado y merecen revisión:</p>
      {''.join(html_rows)}
      <p style="font-size:12px;color:#64748b;margin-top:22px">{html.escape(disclaimer)}</p>
      <p style="font-size:12px;color:#64748b">
        Puedes cambiar o desactivar los avisos desde la aplicación.
      </p>
    </div>
    """
    return subject, "\n".join(plain_lines), html_body


def _overview_display_label(row: DailyOverviewRow) -> str:
    raw_ticker = row.ticker.strip().upper()
    ticker = non_linking_ticker_text(raw_ticker)
    name = row.company_name.strip()
    if not name or name.casefold() == raw_ticker.casefold():
        return ticker
    return f"{name} ({ticker})"


def _overview_decision(row: DailyOverviewRow) -> str:
    """Resume la lectura sin convertir una puntuación aislada en una orden."""

    if (
        row.technical_score is None
        and row.growth_score is None
        and row.fundamental_score is None
        and row.opportunity_score is None
    ):
        return "Datos insuficientes"
    position = row.position_label.strip().casefold()
    if row.held:
        if "vender" in position:
            return "Revisar posible salida"
        if "reduc" in position:
            return "Revisar / reducir"
        if "esper" in position:
            return "Esperar y vigilar"
        return "Mantener / revisar protección"
    if "comprable" in row.opportunity_status.casefold():
        return "Entrada validada para revisar"
    if (
        row.growth_score is not None
        and row.growth_score >= 75
        and (row.technical_score or 0) >= 70
    ):
        return "Crecimiento y momento fuertes; validar precio"
    if (row.technical_score or 0) >= 75:
        return "Momento fuerte; faltan confirmaciones"
    if row.fundamental_score is not None and row.fundamental_score >= 65:
        return "Buena empresa; esperar momento"
    return "Vigilancia"


def _overview_priority(row: DailyOverviewRow) -> tuple[int, int, str]:
    decision = _overview_decision(row)
    if decision == "Revisar posible salida":
        priority = 0
    elif decision == "Revisar / reducir":
        priority = 1
    elif decision == "Entrada validada para revisar":
        priority = 2
    elif "fuertes" in decision or "Momento fuerte" in decision:
        priority = 3
    elif row.changed:
        priority = 4
    elif row.held:
        priority = 5
    else:
        priority = 6
    combined = max(
        row.opportunity_score or 0,
        row.growth_score or 0,
        row.technical_score or 0,
    )
    return priority, -combined, row.ticker


def _overview_change_label(row: DailyOverviewRow) -> str:
    if row.change_kind == "new":
        return "NUEVA · Primera lectura"
    prefixes = {
        "improvement": "MEJORA",
        "deterioration": "DETERIORO",
        "change": "CAMBIO DE ESTADO",
    }
    prefix = prefixes.get(row.change_kind, "")
    if not prefix:
        return ""
    if row.previous_state and row.current_state:
        return f"{prefix} · {row.previous_state} → {row.current_state}"
    return prefix


def build_daily_overview_content(
    display_name: str,
    rows: Iterable[DailyOverviewRow],
) -> tuple[str, str, str]:
    """Crea el segundo correo: todas las empresas, una fila por empresa."""

    values = sorted(list(rows), key=_overview_priority)
    total = len(values)
    buyable = sum(
        "comprable" in row.opportunity_status.casefold() for row in values
    )
    portfolio_reviews = sum(
        row.held
        and _overview_decision(row) in {"Revisar posible salida", "Revisar / reducir"}
        for row in values
    )
    changed = sum(row.changed for row in values)
    new_readings = sum(row.change_kind == "new" for row in values)
    incomplete = sum(
        row.fundamental_score is None or row.growth_score is None for row in values
    )
    subject = f"Stock Signal Lab · resumen diario · {total} empresas revisadas"
    plain_lines = [
        f"Hola {display_name},",
        "",
        "RESUMEN DIARIO COMBINADO",
        f"Empresas revisadas: {total}",
        f"Entradas validadas para revisar: {buyable}",
        f"Posiciones para revisar/reducir/salir: {portfolio_reviews}",
        f"Cambios relevantes desde el análisis anterior: {changed}",
        f"Primeras lecturas: {new_readings}",
        f"Empresas con datos parciales: {incomplete}",
        "",
        "Cada empresa aparece una sola vez. Técnica, crecimiento, fundamentos y "
        "oportunidad son lecturas distintas; ninguna garantiza rentabilidad.",
        "",
        "QUÉ REQUIERE ATENCIÓN HOY",
    ]
    changed_rows = [row for row in values if row.changed]
    if changed_rows:
        plain_lines.append(
            f"Hay {changed} cambio{'s' if changed != 1 else ''} de estado. "
            "Aparecen destacados en la tabla."
        )
    else:
        plain_lines.append("Sin cambios de estado relevantes.")
    plain_lines.extend(["", "TODAS LAS FAVORITAS Y POSICIONES"])
    for row in values:
        scores = (
            f"Técnica {row.technical_score if row.technical_score is not None else 'N/D'} · "
            f"Crecimiento {row.growth_score if row.growth_score is not None else 'N/D'} · "
            f"Fundamental {row.fundamental_score if row.fundamental_score is not None else 'N/D'} · "
            f"Oportunidad {row.opportunity_score if row.opportunity_score is not None else 'N/D'}"
        )
        marker = _overview_change_label(row)
        plain_lines.extend(
            [
                _overview_display_label(row),
                f"{scores} · {_overview_decision(row)}",
            ]
        )
        if marker:
            plain_lines.append(marker)
        if row.data_note:
            plain_lines.append(f"Datos: {row.data_note}")
        plain_lines.append("")

    def score_cell(value: int | None) -> str:
        return "—" if value is None else str(value)

    table_rows = "".join(
        f"""
        <tr style="border-bottom:1px solid #e2e8f0">
          <td style="padding:9px 7px;min-width:150px">
            <strong>{html.escape(_overview_display_label(row))}</strong>
            {f'<br><span style="color:#b7791f;font-size:11px">{html.escape(_overview_change_label(row))}</span>' if _overview_change_label(row) else ''}
          </td>
          <td style="padding:9px 7px;text-align:center">{score_cell(row.technical_score)}</td>
          <td style="padding:9px 7px;text-align:center">{score_cell(row.growth_score)}</td>
          <td style="padding:9px 7px;text-align:center">{score_cell(row.fundamental_score)}</td>
          <td style="padding:9px 7px;text-align:center">{score_cell(row.opportunity_score)}</td>
          <td style="padding:9px 7px;min-width:170px">{html.escape(_overview_decision(row))}</td>
        </tr>
        """
        for row in values
    )
    attention_text = (
        f"Hay {changed} cambio{'s' if changed != 1 else ''} de estado. "
        "Aparecen destacados en la tabla."
        if changed_rows
        else "Sin cambios de estado relevantes."
    )
    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:920px;margin:auto;color:#1e293b">
      <h2 style="color:#14213d">Stock Signal Lab · resumen diario</h2>
      <p>Hola {html.escape(display_name)},</p>
      <div style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:12px;
                  padding:14px 16px;margin:14px 0">
        <strong>{total} empresas revisadas</strong><br>
        Entradas validadas: {buyable} · Cartera para revisar: {portfolio_reviews} ·
        Cambios relevantes: {changed} · Primeras lecturas: {new_readings} ·
        Datos parciales: {incomplete}
      </div>
      <div style="background:#fffaf0;border:1px solid #f6d58a;border-radius:12px;
                  padding:12px 16px;margin:14px 0">
        <strong>Qué requiere atención hoy</strong><br>
        <span style="color:#475569">{html.escape(attention_text)}</span>
      </div>
      <p style="color:#475569">
        Cada empresa aparece una sola vez. Las columnas son lecturas independientes
        y no una probabilidad de ganar.
      </p>
      <div style="overflow-x:auto">
        <table style="border-collapse:collapse;width:100%;font-size:12px">
          <thead>
            <tr style="background:#e8f4ef;color:#0f5132">
              <th style="padding:9px 7px;text-align:left">Empresa</th>
              <th style="padding:9px 7px">Técnica</th>
              <th style="padding:9px 7px">Crecimiento</th>
              <th style="padding:9px 7px">Fundamental</th>
              <th style="padding:9px 7px">Oportunidad</th>
              <th style="padding:9px 7px;text-align:left">Lectura</th>
            </tr>
          </thead>
          <tbody>{table_rows}</tbody>
        </table>
      </div>
      <p style="font-size:12px;color:#64748b;margin-top:22px">
        Son señales probabilísticas basadas en datos históricos. No constituyen
        asesoramiento financiero ni garantizan rentabilidad. Los datos N/D no se
        convierten en una nota negativa.
      </p>
    </div>
    """
    return subject, "\n".join(plain_lines), html_body
