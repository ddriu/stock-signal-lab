"""Lecturas simples que unen señales de mercado y posiciones del usuario."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import pandas as pd


STRONG_ENTRY_LABELS = frozenset({"Entrada fuerte"})
CANDIDATE_ENTRY_LABELS = frozenset({"Entrada interesante", "Entrada candidata"})

_DECISION_PRIORITY = {
    "Revisar posible salida": 0,
    "Revisar exposición": 1,
    "Esperar confirmación": 2,
    "Actualizar datos": 3,
    "Mantener sin ampliar": 4,
    "Posible ampliar": 5,
    "Mantener": 6,
}

_SWITCH_PRIORITY = {
    "Ventaja clara para estudiar": 0,
    "Cambio razonable": 1,
    "Cambio débil": 2,
    "Vigilar": 3,
    "No compensa cambiar": 4,
}


def _ticker(value: object) -> str:
    return str(value or "").strip().upper()


def _number(value: object) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def build_portfolio_decision_rows(
    live_summary: Iterable[Mapping[str, object]],
    held_tickers: Iterable[object],
    *,
    allocations_pct: Mapping[str, float] | None = None,
    max_add_allocation_pct: float = 15.0,
) -> list[dict[str, object]]:
    """Clasifica posiciones con evidencia independiente y lenguaje prudente.

    ``Si ya la tienes`` es una lectura técnica. Por sí sola nunca se convierte
    en una orden de venta: una posible salida exige datos suficientes y varias
    señales adversas adicionales. Así se separa el control de riesgo de una
    verdadera ruptura de tesis, que la app no puede inferir sólo del precio.
    """

    by_ticker = {
        ticker: dict(row)
        for row in live_summary
        if (ticker := _ticker(row.get("Ticker")))
    }
    allocations = {
        _ticker(ticker): float(value)
        for ticker, value in (allocations_pct or {}).items()
        if _number(value) is not None
    }
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for value in held_tickers:
        ticker = _ticker(value)
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        source = by_ticker.get(ticker)
        allocation = allocations.get(ticker)
        if source is None:
            rows.append(
                {
                    "Ticker": ticker,
                    "Prioridad": "Datos pendientes",
                    "Decisión": "Actualizar datos",
                    "Tipo": "Calidad de datos",
                    "Entrada": "Sin comprobar",
                    "Oportunidad": None,
                    "Calidad": None,
                    "Riesgo": None,
                    "Confianza": None,
                    "Peso": allocation,
                    "Motivo": "Falta un precio o análisis reciente.",
                    "Confirmar antes": "Precio, fecha, fundamentales y divisa.",
                    "Riesgo si actúas": "Decidir con datos antiguos o incompletos.",
                    "Horizonte": "No evaluable",
                    "Fecha": None,
                }
            )
            continue

        position_label = str(source.get("Si ya la tienes") or "Revisar")
        entry_label = str(source.get("Lectura entrada") or "Sin comprobar")
        opportunity = _number(source.get("Oportunidad"))
        quality = _number(source.get("Calidad empresa"))
        risk = _number(source.get("Riesgo controlado"))
        confidence = _number(source.get("Confianza datos"))
        allocation_known = allocation is not None
        has_room = allocation_known and allocation < max_add_allocation_pct
        overweight = allocation_known and allocation > max_add_allocation_pct
        attractive_entry = (
            entry_label in STRONG_ENTRY_LABELS.union(CANDIDATE_ENTRY_LABELS)
            and opportunity is not None
            and opportunity >= 65.0
        )
        technical_reason = str(source.get("Motivo posición") or "").strip()
        reliable = confidence is not None and confidence >= 60.0
        supported_for_add = (
            reliable
            and quality is not None
            and quality >= 55.0
            and risk is not None
            and risk >= 50.0
        )
        adverse_evidence = 1  # La propia señal técnica.
        adverse_evidence += int(opportunity is not None and opportunity <= 40.0)
        adverse_evidence += int(quality is not None and quality <= 45.0)
        adverse_evidence += int(risk is not None and risk <= 40.0)
        thesis_invalidated = bool(source.get("Tesis invalidada")) or (
            "ruptura de tesis confirmada" in technical_reason.casefold()
        )
        thesis_reason = str(source.get("Motivo tesis") or "").strip()

        if thesis_invalidated:
            priority = "Alta"
            decision = "Revisar posible salida"
            confirmed_by_readings = (
                position_label == "Vender" and reliable and adverse_evidence >= 3
            )
            decision_type = (
                "Riesgo y tesis confirmados"
                if confirmed_by_readings
                else "Ruptura de tesis declarada"
            )
            reason = (
                thesis_reason
                or (technical_reason if confirmed_by_readings else "")
                or "El usuario ha marcado una invalidación explícita de la tesis."
            )
            confirm = (
                "Contrastar el hecho que cambió la tesis, resultados, guía, caja/deuda "
                "y si el deterioro es permanente."
            )
            action_risk = (
                "La señal aún puede ser ruido; confirma la tesis y el coste fiscal."
                if confirmed_by_readings
                else "Vender sin contrastar el motivo o mantener por inercia una tesis ya rota."
            )
            horizon = "Revisión inmediata"
        elif position_label == "Vender":
            if reliable and adverse_evidence >= 3:
                priority = "Alta"
                decision = "Revisar exposición"
                decision_type = "Riesgo técnico confirmado"
                reason = (
                    technical_reason
                    or "Varias lecturas de precio son adversas, pero no existe una invalidación fundamental independiente."
                )
                confirm = (
                    "Resultados, guía y tesis; reducir sólo si el riesgo o el peso ya no encajan."
                )
                action_risk = "Confundir señales de precio correlacionadas con una ruptura del negocio."
                horizon = "Corto / medio"
            else:
                priority = "Alta"
                decision = "Esperar confirmación"
                decision_type = "Alerta técnica aislada"
                reason = (
                    technical_reason
                    or "La tendencia se ha debilitado, sin evidencia suficiente de ruptura de tesis."
                )
                confirm = (
                    "Cierre técnico confirmado, resultados y tesis; no vender sólo por esta señal."
                )
                action_risk = "Vender por ruido y perder una recuperación posterior."
                horizon = "Corto / medio"
        elif position_label == "Reducir":
            if overweight or (reliable and adverse_evidence >= 2):
                priority = "Media-alta"
                decision = "Revisar exposición"
                decision_type = "Concentración o debilidad"
                reason = (
                    "El peso es elevado y conviene revisar el riesgo."
                    if overweight
                    else technical_reason
                    or "La señal técnica se debilita y otra lectura adversa la acompaña."
                )
                confirm = "Peso objetivo, correlación, tesis y coste fiscal de reducir."
                action_risk = "Reducir demasiado una posición cuya tesis sigue intacta."
            else:
                priority = "Media"
                decision = "Mantener sin ampliar"
                decision_type = "Debilidad técnica no confirmada"
                reason = technical_reason or "La señal técnica pierde fuerza."
                confirm = "Resultados y una segunda confirmación antes de reducir."
                action_risk = "Reaccionar a volatilidad normal de la acción."
            horizon = "Corto / medio"
        elif overweight:
            priority = "Media-alta"
            decision = "Revisar exposición"
            decision_type = "Concentración"
            reason = "La posición supera el peso orientativo para seguir ampliando."
            confirm = "Peso objetivo, correlación sectorial y tolerancia a una caída."
            action_risk = "Concentrar más capital aunque la empresa siga siendo atractiva."
            horizon = "Cartera"
        elif (
            position_label == "Mantener"
            and attractive_entry
            and has_room
            and supported_for_add
        ):
            priority = "Oportunidad"
            decision = "Posible ampliar"
            decision_type = "Entrada con posición sana"
            reason = "Mantiene la tendencia y el precio vuelve a ofrecer una entrada razonable."
            confirm = "Tamaño máximo, resultados próximos y precio límite."
            action_risk = "Aumentar exposición antes de una confirmación fundamental."
            horizon = "Medio / largo"
        elif (
            position_label == "Mantener"
            and attractive_entry
            and allocation_known
            and not has_room
        ):
            priority = "Normal"
            decision = "Mantener"
            decision_type = "Posición completa"
            reason = "La entrada es atractiva, pero el peso actual aconseja no concentrar más."
            confirm = "No requiere acción salvo cambio de tesis."
            action_risk = "Sobreponderar una posición que ya tiene peso suficiente."
            horizon = "Medio / largo"
        elif position_label == "Mantener" and attractive_entry:
            priority = "Datos pendientes"
            decision = "Mantener sin ampliar"
            decision_type = "Peso o evidencia incompletos"
            reason = (
                "La entrada parece atractiva, pero falta un peso fiable o evidencia "
                "suficiente de calidad y riesgo para ampliar."
            )
            confirm = "Peso cotizado, calidad, riesgo y confianza antes de añadir capital."
            action_risk = "Ampliar sin conocer la concentración o la calidad de los datos."
            horizon = "Medio / largo"
        else:
            priority = "Normal"
            decision = "Mantener"
            decision_type = "Sin acción"
            reason = "No hay señal de salida, pero tampoco una entrada suficientemente clara."
            confirm = "Revisar después de resultados o de un cambio material."
            action_risk = "Operar sin una ventaja clara frente a no hacer nada."
            horizon = "Medio / largo"

        rows.append(
            {
                "Ticker": ticker,
                "Prioridad": priority,
                "Decisión": decision,
                "Tipo": decision_type,
                "Entrada": entry_label,
                "Oportunidad": opportunity,
                "Calidad": quality,
                "Riesgo": risk,
                "Confianza": confidence,
                "Peso": allocation,
                "Motivo": reason,
                "Confirmar antes": confirm,
                "Riesgo si actúas": action_risk,
                "Horizonte": horizon,
                "Fecha": source.get("Fecha"),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            _DECISION_PRIORITY.get(str(row.get("Decisión")), 99),
            _ticker(row.get("Ticker")),
        ),
    )


def build_switch_candidate_rows(
    live_summary: Iterable[Mapping[str, object]],
    held_tickers: Iterable[object],
    *,
    minimum_advantage: float = 12.0,
    minimum_confidence: float = 60.0,
    limit: int = 5,
) -> list[dict[str, object]]:
    """Compara cada posición con su mejor alternativa sin recomendar una orden.

    Es un filtro preliminar por notas comparables. Comisiones, fiscalidad, divisa,
    correlación y tesis siguen siendo comprobaciones obligatorias antes de hablar
    de un cambio real de capital.
    """

    normalized = [
        (ticker, dict(row))
        for row in live_summary
        if (ticker := _ticker(row.get("Ticker")))
    ]
    held = {_ticker(value) for value in held_tickers if _ticker(value)}
    by_ticker = {ticker: row for ticker, row in normalized}
    candidates = [
        (ticker, row)
        for ticker, row in normalized
        if ticker not in held and _number(row.get("Oportunidad")) is not None
    ]
    rows: list[dict[str, object]] = []

    for current_ticker in sorted(held):
        current = by_ticker.get(current_ticker)
        current_score = _number(current.get("Oportunidad")) if current else None
        if current is None or current_score is None or not candidates:
            continue
        best_ticker, best = max(
            candidates,
            key=lambda item: _number(item[1].get("Oportunidad")) or -1.0,
        )
        candidate_score = _number(best.get("Oportunidad"))
        if candidate_score is None:
            continue

        advantage = candidate_score - current_score
        current_quality = _number(current.get("Calidad empresa"))
        candidate_quality = _number(best.get("Calidad empresa"))
        current_risk = _number(current.get("Riesgo controlado"))
        candidate_risk = _number(best.get("Riesgo controlado"))
        confidence = _number(best.get("Confianza datos"))
        quality_delta = (
            candidate_quality - current_quality
            if candidate_quality is not None and current_quality is not None
            else None
        )
        risk_delta = (
            candidate_risk - current_risk
            if candidate_risk is not None and current_risk is not None
            else None
        )
        complete = (
            confidence is not None
            and candidate_quality is not None
            and candidate_risk is not None
        )

        if advantage < minimum_advantage:
            classification = "No compensa cambiar"
            reason = "La mejora de atractivo no alcanza el margen mínimo exigido."
        elif not complete or confidence < minimum_confidence:
            classification = "Vigilar"
            reason = "La ventaja aparente existe, pero faltan datos suficientemente fiables."
        elif risk_delta is not None and risk_delta < -10.0:
            classification = "Cambio débil"
            reason = "El mayor potencial exige aceptar bastante más riesgo."
        elif (
            advantage >= 30.0
            and confidence >= 75.0
            and (quality_delta is None or quality_delta >= 0.0)
            and (risk_delta is None or risk_delta >= -5.0)
        ):
            classification = "Ventaja clara para estudiar"
            reason = "La alternativa mejora claramente el atractivo sin degradar calidad o riesgo."
        elif (
            advantage >= 20.0
            and (quality_delta is None or quality_delta >= -10.0)
            and (risk_delta is None or risk_delta >= -10.0)
        ):
            classification = "Cambio razonable"
            reason = "La ventaja es material y el perfil de riesgo sigue siendo comparable."
        else:
            classification = "Cambio débil"
            reason = "Hay ventaja, pero todavía no es suficiente para justificar una rotación."

        rows.append(
            {
                "Posición actual": current_ticker,
                "Alternativa": best_ticker,
                "Lectura": classification,
                "Ventaja": advantage,
                "Atractivo actual": current_score,
                "Atractivo alternativa": candidate_score,
                "Calidad actual": current_quality,
                "Calidad alternativa": candidate_quality,
                "Riesgo actual": current_risk,
                "Riesgo alternativa": candidate_risk,
                "Confianza alternativa": confidence,
                "Motivo": reason,
                "Pendiente antes de cambiar": (
                    "Tesis, correlación, resultados, divisa, comisiones e impacto fiscal."
                ),
                "Fecha": best.get("Fecha"),
            }
        )

    rows.sort(
        key=lambda row: (
            _SWITCH_PRIORITY.get(str(row.get("Lectura")), 99),
            -float(row.get("Ventaja") or 0.0),
            _ticker(row.get("Posición actual")),
        )
    )
    return rows[: max(0, int(limit))]


def entry_opportunity_rows(
    live_summary: Iterable[Mapping[str, object]],
    held_tickers: Iterable[object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Separa entradas fuertes y candidatas que aún no están en cartera."""

    held = {_ticker(value) for value in held_tickers}
    strong: list[dict[str, object]] = []
    candidates: list[dict[str, object]] = []
    for raw_row in live_summary:
        row = dict(raw_row)
        ticker = _ticker(row.get("Ticker"))
        if not ticker or ticker in held:
            continue
        entry_label = str(row.get("Lectura entrada") or "")
        if entry_label in STRONG_ENTRY_LABELS:
            strong.append(row)
        elif entry_label in CANDIDATE_ENTRY_LABELS:
            candidates.append(row)
    sort_key = lambda row: (
        _number(row.get("Momento entrada")) or -1,
        _number(row.get("Oportunidad")) or -1,
    )
    strong.sort(key=sort_key, reverse=True)
    candidates.sort(key=sort_key, reverse=True)
    return strong, candidates
