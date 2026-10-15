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
from math import isfinite
from typing import Iterable
from urllib.parse import urlsplit

from src.signal_engine import (
    LABEL_BUY,
    LABEL_REDUCE,
    LABEL_SELL,
    LABEL_STRONG,
    SignalResult,
)
from src.entry_opportunity import non_linking_ticker_text
from src.portfolio_decisions import build_portfolio_decision_rows


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
    "company_name",
    "growth_score",
    "fundamental_score",
    "opportunity_score",
    "opportunity_status",
    "data_note",
]

DAILY_OVERVIEW_LIMIT = 15


def _html_document(title: str, body: str) -> str:
    """Declara UTF-8 también al abrir una plantilla fuera del cliente de correo."""

    return (
        '<!DOCTYPE html>\n<html lang="es">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{html.escape(title)}</title>\n'
        f'</head>\n<body>\n{body}\n</body>\n</html>\n'
    )


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
    strategy_id: str = ""
    strategy_label: str = ""
    horizon: str = ""
    data_source: str = ""
    data_limitations: str = ""
    next_step: str = ""
    previous_state: str = ""
    current_state: str = ""
    change_reason: str = ""
    policy_snapshot: dict[str, object] | None = None


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
    portfolio_decision: str = ""
    decision_reason: str = ""
    confidence_pct: int | None = None
    strategy_id: str = ""
    strategy_label: str = ""
    horizon: str = ""
    data_source: str = ""
    data_limitations: str = ""
    next_step: str = ""
    policy_snapshot: dict[str, object] | None = None


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
    company_name: str = ""
    growth_score: int | None = None
    fundamental_score: int | None = None
    opportunity_score: int | None = None
    opportunity_status: str = ""
    data_note: str = ""


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
        ranks = {
            LABEL_SELL: 0,
            LABEL_REDUCE: 1,
            "Revisar posible salida": 0,
            "Revisar exposición": 1,
            "Esperar confirmación": 2,
            "Mantener": 2,
        }
        return position, (ranks.get(position, 1), 0)

    if normalized.startswith("entry:"):
        parts = normalized.split(":")
        if len(parts) > 1 and parts[1] == "growth_momentum":
            state = ":".join(parts[2:]) or "Estado principal pendiente"
            growth_ranks = {
                "Entrada fuerte": (4, 2),
                "Entrada candidata": (4, 1),
                "Esperar mejor precio": (3, 0),
                "Vigilancia activa": (2, 0),
                "No preparada": (1, 0),
                "Pendiente de fundamentales": (0, 0),
                "Datos empresariales parciales": (0, 0),
                "Datos empresariales insuficientes": (0, 0),
                "Datos de crecimiento incompletos": (0, 0),
                "Datos insuficientes": (0, 0),
            }
            return state, growth_ranks.get(state, (1, 0))
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
    position_decision: dict[str, object] | None = None,
    strategy_id: str = "",
    strategy_label: str = "",
    horizon: str = "",
    data_source: str = "",
    data_limitations: str = "",
    next_step: str = "",
    previous_signature: str | None = None,
    change_reason: str = "",
    policy_snapshot: dict[str, object] | None = None,
) -> AlertCandidate | None:
    """Aplica las preferencias a una señal ya calculada."""

    primary = (policy_snapshot or {}).get("primary")
    primary_eligible = False
    if not held and policy_snapshot is not None:
        if not isinstance(primary, dict):
            return None
        primary_score = primary.get("score")
        primary_eligible = (
            primary.get("entry_candidate") is True
            and primary.get("data_status") == "complete"
            and isinstance(primary_score, (int, float))
            and not isinstance(primary_score, bool)
            and isfinite(primary_score)
            and primary_score >= preferences.minimum_buy_score
        )
        if not primary_eligible:
            # La técnica auxiliar no suplanta datos ni umbrales del método principal.
            return None
    signature = signal_signature(signal, held=held)
    kind = ""
    title = ""
    decision_label = signal.position_label
    if held:
        decision = position_decision or build_portfolio_decision_rows(
            [{"Ticker": signal.ticker, "Si ya la tienes": signal.position_label}],
            [signal.ticker],
        )[0]
        decision_label = str(decision.get("Decisión") or "Esperar confirmación")
        signature = f"position:{decision_label}"
        if decision_label == "Revisar posible salida" and preferences.alert_sell:
            kind = "Revisión de tesis"
            title = decision_label
        elif decision_label == "Revisar exposición" and (
            preferences.alert_reduce or preferences.alert_sell
        ):
            kind = "Revisión de riesgo"
            title = decision_label
    elif primary_eligible and preferences.alert_buy:
        kind = "Compra"
        title = "Candidata · " + str(primary.get("label") or "Crecimiento + momentum")
        signature = f"entry:growth_momentum:{primary.get('status') or 'Entrada candidata'}"
    elif (
        policy_snapshot is None
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
    _, _, previous_state, current_state = describe_state_change(
        previous_signature, signature, held=held,
    )
    return AlertCandidate(
        ticker=signal.ticker,
        kind=kind,
        title=title,
        entry_score=signal.score,
        entry_label=signal.label,
        position_label=decision_label,
        price=float(price),
        as_of=signal.as_of.date().isoformat(),
        explanation=(
            str(position_decision.get("Motivo") or signal.explanation)
            if held and position_decision is not None
            else str(primary.get("entry_reading") or primary.get("status") or signal.explanation)
            if primary_eligible else signal.explanation
        ),
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
        strategy_id=strategy_id.strip(),
        strategy_label=strategy_label.strip(),
        horizon=horizon.strip(),
        data_source=data_source.strip(),
        data_limitations=data_limitations.strip(),
        next_step=next_step.strip(),
        previous_state=previous_state,
        current_state=current_state,
        change_reason=change_reason.strip(),
        policy_snapshot=policy_snapshot,
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
    company_name: str = "",
    growth_score: int | None = None,
    fundamental_score: int | None = None,
    opportunity_score: int | None = None,
    opportunity_status: str = "",
    data_note: str = "",
    position_label: str | None = None,
) -> AlertState:
    now = evaluated_at or datetime.now().astimezone().isoformat(timespec="seconds")
    return AlertState(
        owner=owner.strip().lower(),
        ticker=signal.ticker.strip().upper(),
        signature=signature or signal_signature(signal, held=held),
        entry_score=int(signal.score),
        entry_label=signal.label,
        position_label=position_label or signal.position_label,
        price=float(price),
        evaluated_at=now,
        # Conserva la última entrega real. De lo contrario, una revisión sin
        # novedades borraría al día siguiente la única evidencia del correo.
        notified_at=now if notified else previous_notified_at,
        company_name=company_name.strip(),
        growth_score=growth_score,
        fundamental_score=fundamental_score,
        opportunity_score=opportunity_score,
        opportunity_status=opportunity_status.strip(),
        data_note=data_note.strip(),
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


def _policy_section(
    row: AlertCandidate | DailyOverviewRow, section: str,
) -> dict[str, object]:
    value = (row.policy_snapshot or {}).get(section)
    return value if isinstance(value, dict) else {}


def _strategy_context(row: AlertCandidate | DailyOverviewRow) -> str:
    position = _policy_section(row, "position")
    strategy = row.strategy_label or str(position.get("strategy_label") or "")
    horizon = row.horizon or str(position.get("horizon") or "")
    return (
        f"Estrategia: {strategy or 'no documentada'} · "
        f"Horizonte: {horizon or 'no documentado'}"
        + (f" · Tesis previa: {position['thesis']}" if position.get("thesis") else "")
    )


def _score_text(value: object) -> str:
    return "N/D" if value is None or value == "" else f"{value}/100"


def _main_evidence(row: AlertCandidate | DailyOverviewRow) -> list[str]:
    primary = _policy_section(row, "primary")
    lines: list[str] = []
    for key, prefix, maximum in (
        ("positive_factors", "Motivos principales (máximo 3; no garantizan rentabilidad)", 3),
        ("risk_factors", "Argumento contrario / riesgos", None),
        ("manual_checks", "Pendientes del método principal", None),
    ):
        values = primary.get(key)
        if isinstance(values, (list, tuple)):
            factors = [str(value) for value in values if value is not None and str(value).strip()]
            if maximum is not None:
                factors = factors[:maximum]
            if factors:
                lines.append(prefix + ": " + "; ".join(factors))
    return lines


def _method_readings(row: AlertCandidate | DailyOverviewRow) -> list[str]:
    """Conserva ambos métodos sin fabricar una media ni un voto de consenso."""

    primary = _policy_section(row, "primary")
    comparative = _policy_section(row, "comparative")
    daily = isinstance(row, DailyOverviewRow)
    technical = row.technical_score if daily else row.entry_score
    technical_label = row.technical_label if daily else row.entry_label
    growth = row.growth_score if daily else None
    fundamental = row.fundamental_score if daily else None
    filter_score = None if daily else row.fundamental_filter_score
    primary_status = str(primary.get("entry_reading") or primary.get("status") or "")
    comparative_reading = str(comparative.get("reading") or "")
    readings = [
        "Método principal · "
        + str(primary.get("label") or "Crecimiento + momentum")
        + ": Crecimiento "
        + _score_text(primary.get("growth_score") if primary else growth)
        + (" · Momentum " if primary else " · Momentum/Técnica ")
        + _score_text(primary.get("momentum_score") if primary else technical)
        + (f" ({technical_label or 'sin lectura'})" if not primary else "")
        + " · Contexto " + _score_text(primary.get("context_score"))
        + (
            " · Puntuación del método " + _score_text(primary["score"])
            if primary.get("score") is not None else ""
        )
        + (f" · Estado: {primary['status']}" if primary.get("status") else "")
        + (f" · Lectura: {primary_status}" if primary_status else ""),
        "Método comparativo · "
        + str(comparative.get("label") or "Calidad / valoración / tesis / riesgo")
        + ": Calidad fundamental — Fundamental "
        + _score_text(comparative.get("quality_score") if comparative else fundamental)
        + " · Valoración " + _score_text(comparative.get("valuation_score"))
        + " · Riesgo " + _score_text(comparative.get("risk_score"))
        + " · Filtro fundamental "
        + _score_text(comparative.get("filter_score") if comparative else filter_score)
        + (
            f" ({row.fundamental_filter_label})"
            if not daily and row.fundamental_filter_label else ""
        )
        + (
            f" ({row.fundamental_label})"
            if daily and row.fundamental_label else ""
        )
        + " · Tesis: " + {
            "not_documented": "no documentada",
            "documented": "documentada; preservar la evidencia previa",
            "invalidated": "invalidación declarada; contrastar el motivo",
        }.get(str(comparative.get("thesis_status")), str(comparative.get("thesis_status") or "no documentada"))
        + (f" · Lectura: {comparative_reading}" if comparative_reading else ""),
    ]
    readings[1:1] = _main_evidence(row)
    if primary:
        readings.append(
            "Técnica de precio auxiliar: Técnica " + _score_text(technical)
            + f" ({technical_label or 'sin lectura'}); no exige consenso con el método principal."
        )
    if row.opportunity_score is not None or row.opportunity_status:
        readings.append(
            "Filtro de entrada auxiliar: Oportunidad "
            + _score_text(row.opportunity_score)
            + (
                " · Timing " + _score_text(row.timing_score)
                if not daily and row.timing_score is not None else ""
            )
            + (f" · {row.opportunity_status}" if row.opportunity_status else "")
            + (
                f" · Zona {row.preferred_entry}"
                if not daily and row.preferred_entry else ""
            )
        )
    if not daily and row.event_label:
        readings.append(f"Evento auxiliar: {row.event_label}")
    if (
        not daily and comparative and row.fundamental_filter_score is not None
        and comparative.get("filter_score") != row.fundamental_filter_score
    ):
        readings.append(
            "Filtro fundamental auxiliar disponible: Filtro fundamental "
            + _score_text(row.fundamental_filter_score)
            + (f" ({row.fundamental_filter_label})" if row.fundamental_filter_label else "")
            + "; independiente de la nota principal."
        )
    if not daily and primary.get("entry_candidate") is True and any(
        word in row.opportunity_status.casefold() for word in ("evento", "esperar", "extendida")
    ):
        readings.append(
            "Contraste de entrada: el método principal identifica una candidata, "
            f"pero el filtro auxiliar señala {row.opportunity_status}. "
            "Se conservan ambas lecturas; la condición pendiente requiere revisión "
            "antes de confirmar una entrada, sin orden automática."
        )
    disagreement = (row.policy_snapshot or {}).get("disagreement")
    if isinstance(disagreement, dict):
        note = str(disagreement.get("reading") or disagreement.get("reason") or disagreement.get("label") or "")
    elif isinstance(disagreement, str):
        note = disagreement
    else:
        note = "Lecturas distintas; revisar ambas" if disagreement else ""
    if note:
        readings.append(f"Divergencia entre métodos: {note}")
    context = _policy_section(row, "context")
    if context.get("cycle") or context.get("seasonality"):
        readings.append(
            "Contexto separado (no crea una entrada ni modifica scores): "
            + " · ".join(str(context[key]) for key in ("cycle", "seasonality") if context.get(key))
        )
    return readings


def _data_context(row: AlertCandidate | DailyOverviewRow) -> list[str]:
    coverage = (
        f" · Cobertura de datos: {row.confidence_pct}%"
        if isinstance(row, DailyOverviewRow) and row.confidence_pct is not None
        else ""
    )
    limitations = row.data_limitations
    if isinstance(row, DailyOverviewRow) and row.data_note:
        limitations = "; ".join(value for value in (limitations, row.data_note) if value)
    lines = [
        f"Fecha del precio: {row.as_of or 'N/D'} · Fuente: "
        f"{row.data_source or 'no documentada'}{coverage}",
        "Limitaciones: " + (
            limitations or "Cobertura y fecha de los fundamentales no verificadas; "
            "la cotización no confirma por sí sola una entrada ni invalida una tesis."
        ),
    ]
    if row.policy_snapshot:
        lines.append(
            "Referencia del análisis: política "
            + str(row.policy_snapshot.get("policy_version") or "N/D")
            + " · Snapshot " + str(row.policy_snapshot.get("snapshot_id") or "N/D")[:12]
            + " (identificador compartido con la aplicación; sin nuevo cálculo)."
        )
    return lines


def _next_step(row: AlertCandidate | DailyOverviewRow) -> str:
    if isinstance(row, AlertCandidate) and "evento" in row.opportunity_status.casefold():
        base = row.next_step or "Validar datos, precio de entrada, horizonte y tamaño antes de decidir."
        return base + " Revisar " + (row.event_label or "el evento señalado") + (
            " y esperar información nueva antes de confirmar una operación."
        )
    if row.next_step:
        return row.next_step
    decision = row.position_label if isinstance(row, AlertCandidate) else _overview_decision(row)
    if not row.held:
        return "Validar datos, precio de entrada, horizonte y tamaño antes de decidir."
    if decision == "Revisar posible salida":
        return "Contrastar la tesis con hechos actualizados antes de decidir mantener o salir."
    if decision == "Revisar exposición":
        return "Revisar concentración, tamaño y tolerancia al riesgo antes de cambiar la posición."
    return "Revisar la tesis y las condiciones pendientes; sin acción automática."


def _candidate_headline(candidate: AlertCandidate) -> str:
    if not candidate.held:
        if "evento" in candidate.opportunity_status.casefold():
            return "Candidata condicionada · revisar el evento antes de la entrada"
        return "Candidata condicionada · revisar entrada"
    if candidate.kind == "Revisión de tesis" or candidate.position_label == "Revisar posible salida":
        return "Revisión de tesis · confirmar antes de decidir"
    return "Revisión de exposición · validar el riesgo"


def _candidate_change(candidate: AlertCandidate) -> str:
    if candidate.previous_state and candidate.current_state:
        if candidate.previous_state == candidate.current_state:
            return "Estado sin cambio de categoría; revisión solicitada por preferencias."
        return f"Antes: {candidate.previous_state} → Ahora: {candidate.current_state}."
    return "Sin estado anterior comparable; primera lectura o historial no disponible."


def _top_candidate_score(candidate: AlertCandidate) -> int | float | None:
    primary = _policy_section(candidate, "primary")
    if primary:
        score = primary.get("score")
        if (
            primary.get("entry_candidate") is True
            and primary.get("data_status") == "complete"
            and isinstance(score, (int, float)) and not isinstance(score, bool)
            and isfinite(score)
        ):
            return score
        return None
    return candidate.opportunity_score


def _top_candidate_reading(candidate: AlertCandidate) -> str:
    primary = _policy_section(candidate, "primary")
    if primary:
        text = f"Nota principal {_score_text(primary.get('score'))}"
        if primary.get("status"):
            text += f" · {primary['status']}"
        if candidate.opportunity_score is not None:
            text += f" · Filtro auxiliar: Oportunidad {_score_text(candidate.opportunity_score)}"
            if candidate.opportunity_status:
                text += f" ({candidate.opportunity_status})"
    else:
        text = f"Oportunidad {_score_text(candidate.opportunity_score)}"
        if candidate.opportunity_status:
            text += f" · {candidate.opportunity_status}"
    if candidate.timing_score is not None:
        text += f" · Timing {_score_text(candidate.timing_score)}"
    if candidate.preferred_entry:
        text += f" · Zona {candidate.preferred_entry}"
    if candidate.event_label:
        text += f" · Evento auxiliar: {candidate.event_label}"
    return text


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
        if all(not candidate.held for candidate in rows):
            subject += " · candidatas condicionadas"
        elif all(candidate.held for candidate in rows):
            subject += " · revisión de cartera"
        else:
            subject += " · candidatas y revisiones"
        subject += f" · {display_label(rows[0])}"
        if len(rows) > 1:
            subject += f" y {len(rows) - 1} más"
    plain_lines = [
        f"Hola {display_name},",
        "",
        "AVISOS PARA REVISIÓN · NO SON ÓRDENES",
        "La decisión requiere validar estrategia, horizonte y datos. Ningún score "
        "es una probabilidad de ganar ni un consenso entre métodos.",
        "",
        "TOP OPORTUNIDADES · CANDIDATAS CONDICIONADAS",
    ]
    opportunity_rows = sorted(
        (
            candidate
            for candidate in rows
            if candidate.kind == "Compra" and _top_candidate_score(candidate) is not None
        ),
        key=lambda candidate: float(_top_candidate_score(candidate) or 0),
        reverse=True,
    )[:5]
    for candidate in opportunity_rows:
        plain_lines.append(
            f"{display_label(candidate)} · Candidata condicionada; revisar precio y datos. "
            f"{_top_candidate_reading(candidate)}"
        )
    if not opportunity_rows:
        plain_lines.append("Sin candidatas nuevas con método principal o filtro de entrada disponible.")
    plain_lines.extend(
        [
            "",
            "QUÉ REVISAR Y POR QUÉ",
            "",
        ]
    )
    html_rows: list[str] = []
    color_by_kind = {
        "Compra": "#16835b",
        "Reducción": "#b7791f",
        "Venta": "#c53030",
        "Revisión de riesgo": "#b7791f",
        "Revisión de tesis": "#c53030",
    }
    for candidate in rows:
        label = display_label(candidate)
        headline = _candidate_headline(candidate)
        reason = candidate.change_reason or candidate.explanation
        method_readings = _method_readings(candidate)
        data_context = _data_context(candidate)
        next_step = _next_step(candidate)
        plain_lines.extend(
            [
                f"{label} · {headline}",
                _strategy_context(candidate),
                f"Qué cambió: {_candidate_change(candidate)}",
                f"Motivo: {reason}",
                f"Siguiente paso: {next_step}",
                "LECTURAS INDEPENDIENTES (puntuaciones de apoyo)",
                *method_readings,
                f"Cierre histórico: {candidate.price:.2f}",
                *data_context,
                "",
            ]
        )
        color = color_by_kind.get(candidate.kind, "#2563eb")
        detail_lines = [
            _strategy_context(candidate),
            f"Qué cambió: {_candidate_change(candidate)}",
            f"Motivo: {reason}",
            f"Siguiente paso: {next_step}",
        ]
        details_html = "".join(
            f'<p style="margin:0 0 8px">{html.escape(line)}</p>'
            for line in detail_lines
        )
        readings_html = "".join(
            f'<p style="margin:4px 0;color:#475569">{html.escape(line)}</p>'
            for line in method_readings
        )
        data_html = "".join(
            f'<p style="margin:4px 0;color:#64748b">{html.escape(line)}</p>'
            for line in data_context
        )
        html_rows.append(
            f"""
            <div style="border:1px solid #e2e8f0;border-left:5px solid {color};
                        border-radius:10px;padding:14px 16px;margin:12px 0">
              <div style="font-size:12px;color:{color};font-weight:700">
                {html.escape('CANDIDATA CONDICIONADA' if not candidate.held else candidate.kind.upper())}
              </div>
              <h3 style="margin:4px 0 8px;color:#14213d">
                {html.escape(label)} · {html.escape(headline)}
              </h3>
              {details_html}
              <div style="background:#f8fafc;padding:8px;border-radius:6px">
                <strong>Lecturas independientes · puntuaciones de apoyo</strong>
                {readings_html}
              </div>
              <p style="margin:8px 0 4px">Cierre histórico: {candidate.price:.2f}</p>
              {data_html}
            </div>
            """
        )
    disclaimer = (
        "Son señales probabilísticas basadas en datos históricos. No constituyen "
        "asesoramiento financiero ni garantizan rentabilidad. Este correo no ordena "
        "comprar, reducir ni vender; requiere una decisión manual."
    )
    plain_lines.extend([disclaimer, "Puedes desactivar estos avisos desde la aplicación."])
    html_opportunity_rows = "".join(
        '<p style="margin:8px 0">'
        + html.escape(display_label(candidate))
        + " · Candidata condicionada; revisar precio y datos."
        + '<br><span style="color:#475569">'
        + html.escape(_top_candidate_reading(candidate))
        + "</span></p>"
        for candidate in opportunity_rows
    )
    if not html_opportunity_rows:
        html_opportunity_rows = (
            '<p style="margin:8px 0;color:#64748b">'
            "Sin candidatas nuevas con método principal o filtro de entrada disponible.</p>"
        )
    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:680px;margin:auto;color:#1e293b">
      <h2 style="color:#14213d">Stock Signal Lab</h2>
      <p>Hola {html.escape(display_name)},</p>
      <p><strong>Avisos para revisión · no son órdenes</strong><br>
        Validar estrategia, horizonte y datos antes de decidir. Los scores no son
        una probabilidad de ganar ni un consenso entre métodos.
      </p>
      <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;
                  padding:12px 16px;margin:12px 0">
        <strong>TOP OPORTUNIDADES · CANDIDATAS CONDICIONADAS</strong>
        {html_opportunity_rows}
      </div>
      <p>Qué revisar y por qué:</p>
      {''.join(html_rows)}
      <p style="font-size:12px;color:#64748b;margin-top:22px">{html.escape(disclaimer)}</p>
      <p style="font-size:12px;color:#64748b">
        Puedes cambiar o desactivar los avisos desde la aplicación.
      </p>
    </div>
    """
    return subject, "\n".join(plain_lines), _html_document(subject, html_body)


def _overview_display_label(row: DailyOverviewRow) -> str:
    raw_ticker = row.ticker.strip().upper()
    ticker = non_linking_ticker_text(raw_ticker)
    name = row.company_name.strip()
    if not name or name.casefold() == raw_ticker.casefold():
        return ticker
    return f"{name} ({ticker})"


def _overview_decision(row: DailyOverviewRow) -> str:
    """Resume la lectura sin convertir una puntuación aislada en una orden."""

    if row.portfolio_decision:
        return row.portfolio_decision
    if (
        row.technical_score is None
        and row.growth_score is None
        and row.fundamental_score is None
        and row.opportunity_score is None
    ):
        return "Datos insuficientes"
    management = _policy_section(row, "position").get("management_reading")
    if row.held and management:
        return str(management)
    if row.held:
        decision = build_portfolio_decision_rows(
            [{
                "Ticker": row.ticker,
                "Si ya la tienes": row.position_label,
                "Lectura entrada": row.technical_label,
                "Oportunidad": row.opportunity_score,
                "Calidad empresa": row.fundamental_score,
                "Confianza datos": row.confidence_pct,
            }],
            [row.ticker],
        )[0]
        return str(decision["Decisión"])
    primary = _policy_section(row, "primary")
    if primary and (
        primary.get("entry_candidate") is False
        or primary.get("data_status") in {"pending", "partial"}
    ):
        return str(primary.get("entry_reading") or "Datos insuficientes para validar entrada")
    if (
        "comprable" in row.opportunity_status.casefold()
        and row.confidence_pct is not None
        and row.confidence_pct >= 60
    ):
        return "Entrada validada para revisar"
    primary_reading = primary.get("entry_reading")
    if primary_reading:
        return str(primary_reading)
    if (row.technical_score or 0) >= 75:
        return "Momento fuerte; faltan confirmaciones"
    if row.fundamental_score is not None and row.fundamental_score >= 65:
        return "Buena empresa; esperar momento"
    return "Vigilancia"


def _overview_headline(row: DailyOverviewRow) -> str:
    decision = _overview_decision(row)
    if decision == "Datos insuficientes":
        return decision + " · no valorar una operación"
    if row.held:
        if decision == "Revisar posible salida":
            return "Revisión de tesis · confirmar antes de decidir"
        if decision == "Revisar exposición":
            return "Revisión de exposición · validar el riesgo"
        return decision
    if decision == "Vigilancia":
        return "Vigilar · sin entrada confirmada"
    primary = _policy_section(row, "primary")
    if primary:
        candidate = primary.get("entry_candidate") is True and primary.get("data_status") not in {"pending", "partial"}
    else:
        candidate = decision == "Entrada validada para revisar" or "Momento fuerte" in decision
    return ("Candidata condicionada · " if candidate else "Vigilar · ") + decision


def _overview_priority(row: DailyOverviewRow) -> tuple[int, int, str]:
    decision = _overview_decision(row)
    primary = _policy_section(row, "primary")
    primary_candidate = (
        primary.get("entry_candidate") is True
        and primary.get("data_status") == "complete"
    )
    if row.held and decision.startswith("Revisar posible salida"):
        priority = 0
    elif row.held and decision.startswith("Revisar"):
        priority = 1
    elif row.changed:
        priority = 2
    elif primary_candidate or decision == "Entrada validada para revisar" or "Momento fuerte" in decision:
        priority = 3
    elif row.held:
        priority = 4
    elif row.change_kind == "new":
        priority = 5
    else:
        priority = 6
    # Desempata por el método principal, nunca por una mezcla de métodos.
    primary_score = primary.get("score")
    if not isinstance(primary_score, (int, float)) or isinstance(primary_score, bool) or not isfinite(primary_score):
        primary_score = row.growth_score if row.growth_score is not None else row.technical_score or 0
    return priority, -int(primary_score), row.ticker


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
    *,
    app_url: str = "",
) -> tuple[str, str, str]:
    """Recuentos completos y extracto priorizado; nunca más de quince empresas."""

    values = sorted(list(rows), key=_overview_priority)
    total = len(values)
    selected = values[:DAILY_OVERVIEW_LIMIT]
    omitted = total - len(selected)
    try:
        parsed_url = urlsplit(app_url.strip())
        safe_app_url = (
            app_url.strip()
            if parsed_url.scheme in {"http", "https"} and parsed_url.hostname
            and not parsed_url.username and not parsed_url.password
            and "\r" not in app_url and "\n" not in app_url else ""
        )
    except ValueError:
        safe_app_url = ""
    detail_text = (
        f"Favoritas y posiciones completas en la aplicación: {safe_app_url}"
        if safe_app_url else "Favoritas y posiciones completas: abre Stock Signal Lab; "
        "la URL de la aplicación no está configurada en este correo."
    )
    extract_text = f"Extracto priorizado: {len(selected)} de {total} empresas (máximo {DAILY_OVERVIEW_LIMIT})."
    omitted_text = f"Empresas fuera del extracto: {omitted}; sí están incluidas en los recuentos."
    buyable = sum(_overview_decision(row) == "Entrada validada para revisar" for row in values)
    portfolio_reviews = sum(
        row.held
        and _overview_decision(row) in {"Revisar posible salida", "Revisar exposición"}
        for row in values
    )
    changed = sum(row.changed for row in values)
    new_readings = sum(row.change_kind == "new" for row in values)
    incomplete = sum(
        row.fundamental_score is None or row.growth_score is None for row in values
    )
    priced = sum(row.technical_score is not None for row in values)
    unavailable = total - priced
    subject = (
        f"Stock Signal Lab · resumen diario · {priced}/{total} empresas procesadas"
    )
    plain_lines = [
        f"Hola {display_name},",
        "",
        "RESUMEN DIARIO · MÉTODOS INDEPENDIENTES",
        f"Empresas solicitadas: {total}",
        f"Empresas procesadas con precios: {priced}",
        f"Empresas sin precio o sin análisis: {unavailable}",
        f"Entradas validadas para revisar: {buyable}",
        f"Posiciones para revisar (sin orden de venta): {portfolio_reviews}",
        f"Cambios relevantes desde el análisis anterior: {changed}",
        f"Primeras lecturas: {new_readings}",
        f"Empresas con datos parciales: {incomplete}",
        "",
        extract_text,
        omitted_text,
        detail_text,
        "",
        "Cada empresa mostrada aparece una sola vez en el extracto. El método principal es crecimiento + "
        "momentum; el comparativo muestra calidad, valoración, tesis y riesgo. "
        "No se suman entre sí ni forman una orden o un consenso. "
        "Las puntuaciones son apoyo, no probabilidades de rentabilidad.",
        "",
        "QUÉ REQUIERE ATENCIÓN HOY",
    ]
    changed_rows = [row for row in values if row.changed]
    if changed_rows:
        plain_lines.append(
            f"Hay {changed} cambio{'s' if changed != 1 else ''} de estado. "
            "Se priorizan en el extracto; consulta el resto en la aplicación."
        )
    else:
        plain_lines.append("Sin cambios de estado relevantes.")
    plain_lines.extend(["", "EXTRACTO PRIORIZADO DE FAVORITAS Y POSICIONES"])
    for row in selected:
        marker = _overview_change_label(row)
        plain_lines.extend(
            [
                f"{_overview_display_label(row)} · {_overview_headline(row)}",
                _strategy_context(row),
                f"Siguiente paso: {_next_step(row)}",
            ]
        )
        if row.decision_reason:
            plain_lines.append(f"Motivo: {row.decision_reason}")
        if marker:
            plain_lines.append(f"Qué cambió: {marker}")
        elif row.changed:
            plain_lines.append("Qué cambió: cambio marcado, sin estado anterior comparable.")
        else:
            plain_lines.append("Qué cambió: sin cambio de categoría confirmado.")
        plain_lines.extend(_method_readings(row))
        plain_lines.extend(_data_context(row))
        plain_lines.append("")
    plain_lines.append(
        "No constituyen asesoramiento financiero ni garantizan rentabilidad. "
        "Este correo no ordena comprar, reducir ni vender; toda decisión es manual. "
        "Los datos N/D no se convierten en una nota negativa."
    )

    html_rows: list[str] = []
    for row in selected:
        marker = _overview_change_label(row)
        readings_html = "".join(
            f'<p style="margin:0 0 7px">{html.escape(line)}</p>'
            for line in _method_readings(row)
        )
        data_html = "".join(
            f'<p style="margin:4px 0;color:#64748b">{html.escape(line)}</p>'
            for line in _data_context(row)
        )
        reason_html = (
            f'<p style="margin:4px 0">Motivo: {html.escape(row.decision_reason)}</p>'
            if row.decision_reason else ""
        )
        change_text = marker or (
            "Cambio marcado, sin estado anterior comparable."
            if row.changed else "Sin cambio de categoría confirmado."
        )
        html_rows.append(
            f"""
            <tr style="border-bottom:1px solid #e2e8f0;vertical-align:top">
              <td style="padding:12px 9px;min-width:145px;width:20%">
                <strong>{html.escape(_overview_display_label(row))}</strong>
                <p style="margin:4px 0;color:#b7791f">Qué cambió: {html.escape(change_text)}</p>
              </td>
              <td style="padding:12px 9px;min-width:200px;width:35%">
                <strong>{html.escape(_overview_headline(row))}</strong>
                <p style="margin:4px 0">{html.escape(_strategy_context(row))}</p>
                {reason_html}
                <p style="margin:4px 0">Siguiente paso: {html.escape(_next_step(row))}</p>
                {data_html}
              </td>
              <td style="padding:12px 9px;min-width:240px;width:45%;color:#475569">
                {readings_html}
              </td>
            </tr>
            """
        )
    table_rows = "".join(html_rows)
    attention_text = (
        f"Hay {changed} cambio{'s' if changed != 1 else ''} de estado. "
        "Se priorizan en el extracto; consulta el resto en la aplicación."
        if changed_rows
        else "Sin cambios de estado relevantes."
    )
    app_link_html = (
        f'<a href="{html.escape(safe_app_url, quote=True)}">Abrir favoritas y posiciones completas en Stock Signal Lab</a>'
        if safe_app_url else html.escape(detail_text)
    )
    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:920px;margin:auto;color:#1e293b">
      <h2 style="color:#14213d">Stock Signal Lab · resumen diario</h2>
      <p>Hola {html.escape(display_name)},</p>
      <div style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:12px;
                  padding:14px 16px;margin:14px 0">
        <strong>{priced} de {total} empresas procesadas con precios</strong><br>
        Sin precio o sin análisis: {unavailable} ·
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
        Cada empresa mostrada aparece una sola vez en el extracto. Crecimiento + momentum es el método
        principal; calidad / valoración / tesis / riesgo es el comparativo.
        Para las empresas mostradas, se conservan las lecturas independientes:
        no se suman ni forman un consenso.
        Los scores son apoyo, no una probabilidad de ganar ni una orden.
      </p>
      <p><strong>{html.escape(extract_text)}</strong><br>
        {html.escape(omitted_text)}<br>{app_link_html}
      </p>
      <div style="overflow-x:auto">
        <table style="border-collapse:collapse;width:100%;font-size:12px">
          <thead>
            <tr style="background:#e8f4ef;color:#0f5132">
              <th style="padding:9px 7px;text-align:left">Empresa</th>
              <th style="padding:9px 7px;text-align:left">Qué revisar · datos · siguiente paso</th>
              <th style="padding:9px 7px;text-align:left">Métodos independientes · puntuaciones de apoyo</th>
            </tr>
          </thead>
          <tbody>{table_rows}</tbody>
        </table>
      </div>
      <p style="font-size:12px;color:#64748b;margin-top:22px">
        Son señales probabilísticas basadas en datos históricos. No constituyen
        asesoramiento financiero ni garantizan rentabilidad. Los datos N/D no se
        convierten en una nota negativa. Este correo no ordena comprar, reducir
        ni vender; toda decisión es manual y requiere validar la tesis y el horizonte.
      </p>
    </div>
    """
    return subject, "\n".join(plain_lines), _html_document(subject, html_body)
