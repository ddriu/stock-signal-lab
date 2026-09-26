"""Mapa prudente de cartera y candidatas por horizonte.

Este módulo no genera órdenes. Une las familias de análisis ya existentes para
responder tres preguntas separadas: qué mantener, qué vigilar y qué cambios
merecen un estudio de costes y tesis. La ausencia de datos nunca se convierte
en una señal de compra o venta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping

import pandas as pd

from src.data_loader import resolve_analysis_ticker
from src.portfolio_decisions import build_portfolio_decision_rows


ROTATION_COLORS: dict[str, dict[str, str]] = {
    "Azul": {"hex": "#2776D2", "meaning": "Posible destino de capital"},
    "Verde": {"hex": "#16875A", "meaning": "Mantener"},
    "Amarillo": {"hex": "#D7A514", "meaning": "Esperar o vigilar"},
    "Naranja": {"hex": "#E67E22", "meaning": "Revisar peso o posible rotación"},
    "Rojo": {"hex": "#C2413A", "meaning": "Revisar posible salida"},
    "Gris": {"hex": "#7C8798", "meaning": "Datos insuficientes o antiguos"},
}


@dataclass(frozen=True)
class HorizonPolicy:
    label: str
    weights: Mapping[str, float]
    minimum_confidence: float
    candidate_score: float
    minimum_advantage: float
    maximum_recovery_hurdle_pct: float
    maximum_age_days: int
    normal_rotations: bool
    maximum_switches: int
    cadence: str


HORIZON_POLICIES: dict[str, HorizonPolicy] = {
    "Diario": HorizonPolicy(
        label="Diario",
        weights={"Momento entrada": 50, "Fuerza relativa": 25, "Riesgo controlado": 25},
        minimum_confidence=80,
        candidate_score=80,
        minimum_advantage=30,
        maximum_recovery_hurdle_pct=2.0,
        maximum_age_days=3,
        normal_rotations=False,
        maximum_switches=0,
        cadence="Vigilar hechos y riesgo; no rotar por el ruido de una sesión.",
    ),
    "Semanal": HorizonPolicy(
        label="Semanal",
        weights={
            "Momento entrada": 35,
            "Fuerza relativa": 30,
            "Calidad empresa": 10,
            "Valoración": 5,
            "Riesgo controlado": 20,
        },
        minimum_confidence=75,
        candidate_score=75,
        minimum_advantage=20,
        maximum_recovery_hurdle_pct=3.0,
        maximum_age_days=7,
        normal_rotations=True,
        maximum_switches=1,
        cadence="Confirmar persistencia; como máximo estudiar un cambio pequeño por semana.",
    ),
    "Mensual": HorizonPolicy(
        label="Mensual",
        weights={
            "Momento entrada": 20,
            "Fuerza relativa": 30,
            "Calidad empresa": 25,
            "Valoración": 10,
            "Riesgo controlado": 15,
        },
        minimum_confidence=70,
        candidate_score=70,
        minimum_advantage=15,
        maximum_recovery_hurdle_pct=5.0,
        maximum_age_days=14,
        normal_rotations=True,
        maximum_switches=3,
        cadence="Horizonte preferente para rebalancear por tramos, costes y concentración.",
    ),
    "Anual": HorizonPolicy(
        label="Anual",
        weights={
            "Momento entrada": 5,
            "Fuerza relativa": 15,
            "Calidad empresa": 40,
            "Valoración": 25,
            "Riesgo controlado": 15,
        },
        minimum_confidence=70,
        candidate_score=70,
        minimum_advantage=12,
        maximum_recovery_hurdle_pct=8.0,
        maximum_age_days=45,
        normal_rotations=True,
        maximum_switches=3,
        cadence="Revisar tesis, calidad, valoración, asignación y fiscalidad.",
    ),
}


@dataclass(frozen=True)
class RotationDashboard:
    positions: tuple[dict[str, object], ...]
    candidates: tuple[dict[str, object], ...]
    switches: tuple[dict[str, object], ...]
    policy: HorizonPolicy


def _ticker(value: object) -> str:
    return str(value or "").strip().upper()


def _number(value: object) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_date(value: object) -> date | None:
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(parsed):
        return None
    return parsed.date()


def build_pair_correlations(
    prepared: Mapping[str, pd.DataFrame],
    current_tickers: Iterable[object],
    candidate_tickers: Iterable[object],
    *,
    sessions: int = 126,
) -> dict[tuple[str, str], float]:
    """Correlaciones recientes; la falta de histórico queda sin rellenar."""

    currents = [_ticker(value) for value in current_tickers if _ticker(value)]
    candidates = [_ticker(value) for value in candidate_tickers if _ticker(value)]
    returns: dict[str, pd.Series] = {}
    for ticker in set([*currents, *candidates]):
        frame = prepared.get(ticker)
        if frame is None or frame.empty or "close" not in frame:
            continue
        series = pd.to_numeric(frame["close"], errors="coerce").pct_change(
            fill_method=None
        ).dropna().tail(sessions)
        if len(series) >= 40:
            returns[ticker] = series
    result: dict[tuple[str, str], float] = {}
    for current in currents:
        for candidate in candidates:
            if current not in returns or candidate not in returns:
                continue
            aligned = pd.concat(
                [returns[current], returns[candidate]], axis=1, join="inner"
            ).dropna()
            if len(aligned) < 40:
                continue
            correlation = aligned.iloc[:, 0].corr(aligned.iloc[:, 1])
            if pd.notna(correlation):
                result[(current, candidate)] = float(correlation)
    return result


def build_recovery_hurdles(
    open_positions: pd.DataFrame,
    live_summary: Iterable[Mapping[str, object]],
    candidate_tickers: Iterable[object],
    *,
    snapshot: pd.DataFrame | None = None,
    tax_rate_pct: float,
    sell_fee_eur: float,
    buy_fee_eur: float,
    spread_pct: float,
    fx_cost_pct: float,
) -> dict[tuple[str, str], dict[str, object]]:
    """Calcula fricción y capital reinvertible sin mezclarlos con el score."""

    for value, label in (
        (tax_rate_pct, "impuesto"),
        (sell_fee_eur, "comisión de venta"),
        (buy_fee_eur, "comisión de compra"),
        (spread_pct, "spread"),
        (fx_cost_pct, "divisa"),
    ):
        if value < 0:
            raise ValueError(f"El supuesto de {label} no puede ser negativo.")
    summary = {
        _ticker(row.get("Ticker")): dict(row)
        for row in live_summary
        if _ticker(row.get("Ticker"))
    }
    currencies = {
        ticker: str(row.get("Moneda") or "").strip().upper()
        for ticker, row in summary.items()
    }
    contexts: dict[str, dict[str, float | str]] = {}
    required_open = {
        "Ticker",
        "Ahora vale",
        "Coste de vender",
        "Recibirías si vendieras",
        "Impuesto aproximado",
        "Capital neto disponible",
    }
    if not open_positions.empty and required_open.issubset(open_positions.columns):
        frame = open_positions.copy()
        frame["Ticker normalizado"] = frame["Ticker"].fillna("").astype(str).map(
            lambda value: resolve_analysis_ticker(value) if value.strip() else ""
        )
        value_columns = list(required_open.difference({"Ticker"}))
        for column in value_columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
        grouped = frame.groupby("Ticker normalizado", as_index=False)[value_columns].sum()
        for _, current in grouped.iterrows():
            contexts[str(current["Ticker normalizado"])] = {
                "Valor bruto": float(current["Ahora vale"]),
                "Coste de vender": float(current["Coste de vender"]),
                "Efectivo bróker": float(current["Recibirías si vendieras"]),
                "Reserva fiscal": float(current["Impuesto aproximado"]),
                "Capital neto": float(current["Capital neto disponible"]),
                "Origen": "Operaciones FIFO",
            }

    snapshot_frame = snapshot.copy() if snapshot is not None else pd.DataFrame()
    required_snapshot = {"analysis_ticker", "value_eur", "cost_estimate_eur"}
    if not snapshot_frame.empty and required_snapshot.issubset(snapshot_frame.columns):
        snapshot_frame["analysis_ticker"] = (
            snapshot_frame["analysis_ticker"]
            .fillna("")
            .astype(str)
            .str.strip()
            .str.upper()
            .map(lambda ticker: resolve_analysis_ticker(ticker) if ticker else "")
        )
        snapshot_frame["value_eur"] = pd.to_numeric(
            snapshot_frame["value_eur"], errors="coerce"
        )
        snapshot_frame["cost_estimate_eur"] = pd.to_numeric(
            snapshot_frame["cost_estimate_eur"], errors="coerce"
        )
        analyzable = snapshot_frame.loc[snapshot_frame["analysis_ticker"] != ""]
        for ticker, ticker_rows in analyzable.groupby("analysis_ticker"):
            source_text = " ".join(
                ticker_rows.get(
                    "source", pd.Series("", index=ticker_rows.index)
                ).fillna("").astype(str)
            ).casefold()
            reconciled_from_journal = "diario de operaciones" in source_text
            if ticker in contexts and reconciled_from_journal:
                continue
            gross_value = float(ticker_rows["value_eur"].fillna(0.0).sum())
            costs = ticker_rows["cost_estimate_eur"]
            if gross_value <= 0 or not costs.notna().all():
                continue
            cost_basis = float(costs.sum())
            current_currency = currencies.get(str(ticker), "")
            if not current_currency:
                continue
            sell_variable_pct = spread_pct + (
                fx_cost_pct if current_currency != "EUR" else 0.0
            )
            sale_cost = sell_fee_eur + gross_value * sell_variable_pct / 100.0
            broker_cash = max(0.0, gross_value - sale_cost)
            tax_reserve = max(0.0, broker_cash - cost_basis) * tax_rate_pct / 100.0
            contexts[str(ticker)] = {
                "Valor bruto": gross_value,
                "Coste de vender": sale_cost,
                "Efectivo bróker": broker_cash,
                "Reserva fiscal": tax_reserve,
                "Capital neto": max(0.0, broker_cash - tax_reserve),
                "Origen": (
                    "Coste reconciliado del diario"
                    if reconciled_from_journal
                    else "Coste aproximado de la fotografía"
                ),
            }

    results: dict[tuple[str, str], dict[str, object]] = {}
    candidates = [_ticker(value) for value in candidate_tickers if _ticker(value)]
    for current_ticker, context in contexts.items():
        gross_value = float(context["Valor bruto"])
        broker_cash = float(context["Efectivo bróker"])
        tax_reserve = float(context["Reserva fiscal"])
        net_available = float(context["Capital neto"])
        sale_cost = float(context["Coste de vender"])
        if gross_value <= 0 or net_available <= buy_fee_eur:
            continue
        for candidate in candidates:
            currency = currencies.get(candidate, "")
            if not currency:
                continue
            variable_pct = spread_pct + (fx_cost_pct if currency != "EUR" else 0.0)
            purchasable = max(
                0.0,
                (net_available - buy_fee_eur) / (1.0 + variable_pct / 100.0),
            )
            if purchasable <= 0:
                continue
            buy_cost = max(0.0, net_available - purchasable)
            results[(current_ticker, candidate)] = {
                "Valor bruto": gross_value,
                "Efectivo bróker": broker_cash,
                "Reserva fiscal": tax_reserve,
                "Capital prudente": purchasable,
                "Fricción económica": sale_cost + buy_cost,
                "Umbral recuperación": (gross_value / purchasable - 1.0) * 100.0,
                "Origen coste": str(context["Origen"]),
            }
    return results


def horizon_score(
    row: Mapping[str, object],
    horizon: str,
    *,
    today: date | None = None,
) -> dict[str, object]:
    """Calcula una nota sin contar dos veces ``Oportunidad`` y sus componentes."""

    if horizon not in HORIZON_POLICIES:
        raise ValueError(f"Horizonte no reconocido: {horizon}")
    policy = HORIZON_POLICIES[horizon]
    available_weight = 0.0
    weighted_score = 0.0
    for column, weight in policy.weights.items():
        value = _number(row.get(column))
        if value is None:
            continue
        available_weight += float(weight)
        weighted_score += float(weight) * min(100.0, max(0.0, value))
    coverage = available_weight / sum(policy.weights.values()) * 100.0
    raw_score = weighted_score / available_weight if available_weight else None

    stated_confidence = _number(row.get("Confianza datos"))
    confidence = min(coverage, stated_confidence if stated_confidence is not None else coverage)
    as_of = _as_date(row.get("Fecha"))
    age_days = (today or date.today()) - as_of if as_of is not None else None
    stale = (
        as_of is None
        or age_days.days < 0
        or age_days.days > policy.maximum_age_days
    )
    if stale:
        confidence = min(confidence, 49.0)
    effective = (
        50.0 + (raw_score - 50.0) * confidence / 100.0
        if raw_score is not None
        else None
    )
    return {
        "Score bruto": raw_score,
        "Score horizonte": effective,
        "Cobertura": coverage,
        "Confianza": confidence,
        "Fecha": as_of,
        "Antigüedad": age_days.days if age_days is not None else None,
        "Datos antiguos": stale,
    }


def _position_color(
    decision: str,
    score: Mapping[str, object],
    policy: HorizonPolicy,
) -> tuple[str, str]:
    stale = bool(score.get("Datos antiguos"))
    effective = _number(score.get("Score horizonte"))
    confidence = _number(score.get("Confianza"))
    coverage = _number(score.get("Cobertura")) or 0.0
    if decision == "Revisar posible salida":
        return "Rojo", "Revisar tesis y posible salida; no vender automáticamente"
    if stale or decision == "Actualizar datos":
        return "Gris", "Actualizar antes de decidir"
    if decision == "Revisar exposición":
        return "Naranja", "Revisar peso o usar sólo como posible origen de rotación"
    if effective is None or confidence is None:
        return "Gris", "Completar el análisis antes de decidir"
    if confidence < policy.minimum_confidence or coverage < 80.0:
        return "Gris", "Datos insuficientes para una decisión con este horizonte"
    if coverage < 100.0:
        return "Amarillo", "Falta completar algún factor antes de actuar"
    if decision in {"Esperar confirmación", "Mantener sin ampliar"}:
        return "Amarillo", "Mantener sin ampliar y esperar confirmación"
    if decision == "Posible ampliar":
        if effective >= policy.candidate_score:
            return "Azul", "Posible ampliar si el peso y el precio lo permiten"
        return "Amarillo", "El horizonte elegido no confirma todavía una ampliación"
    if effective < 50.0:
        return "Amarillo", "Mantener y revisar; el horizonte elegido no ofrece ventaja clara"
    return "Verde", "Mantener; no hay una acción con ventaja suficiente"


def _candidate_color(
    source: Mapping[str, object],
    score: Mapping[str, object],
    policy: HorizonPolicy,
) -> tuple[str, str]:
    entry = str(source.get("Lectura entrada") or "")
    effective = _number(score.get("Score horizonte"))
    confidence = _number(score.get("Confianza")) or 0.0
    quality = _number(source.get("Calidad empresa"))
    risk = _number(source.get("Riesgo controlado"))
    entry_is_valid = entry in {"Entrada fuerte", "Entrada interesante", "Entrada candidata"}
    if bool(score.get("Datos antiguos")) or effective is None:
        return "Gris", "Actualizar datos antes de considerar una entrada"
    if quality is None or risk is None:
        return "Gris", "Completar calidad y riesgo antes de considerar una entrada"
    missing_factors = [
        factor for factor in policy.weights if _number(source.get(factor)) is None
    ]
    if missing_factors:
        return "Gris", "Completar todos los factores del horizonte antes de entrar"
    if (
        entry_is_valid
        and effective >= policy.candidate_score
        and confidence >= policy.minimum_confidence
        and quality >= 55
        and risk >= 50
    ):
        return "Azul", "Favorita que puede recibir capital; falta validar precio y tamaño"
    return "Amarillo", "Favorita en vigilancia; todavía no cumple todos los filtros"


def _switch_label(
    *,
    advantage: float,
    confidence: float,
    hurdle: float | None,
    correlation: float | None,
    policy: HorizonPolicy,
) -> str:
    """Gradúa sólo pares que ya han superado todos los filtros obligatorios."""

    if (
        advantage >= policy.minimum_advantage + 15
        and confidence >= 85
        and hurdle is not None
        and hurdle <= policy.maximum_recovery_hurdle_pct / 2
        and correlation is not None
        and correlation < 0.75
    ):
        return "Ventaja clara para estudiar"
    if (
        advantage >= policy.minimum_advantage + 7
        and confidence >= 80
        and hurdle is not None
        and hurdle <= policy.maximum_recovery_hurdle_pct * 0.75
    ):
        return "Cambio razonable"
    return "Cambio débil"


def build_rotation_dashboard(
    live_summary: Iterable[Mapping[str, object]],
    held_tickers: Iterable[object],
    favorite_tickers: Iterable[object],
    *,
    allocations_pct: Mapping[str, float] | None = None,
    horizon: str = "Mensual",
    recovery_hurdles: Mapping[tuple[str, str], Mapping[str, object]] | None = None,
    pair_correlations: Mapping[tuple[str, str], float] | None = None,
    max_company_weight_pct: float = 15.0,
    max_sector_weight_pct: float = 25.0,
    require_costs: bool = True,
    limit_candidates: int = 15,
    limit_switches: int = 10,
    today: date | None = None,
) -> RotationDashboard:
    """Construye posiciones, favoritas candidatas y cambios que merecen estudio.

    Las alternativas se limitan estrictamente a favoritas. Un salto sólo aparece
    cuando la posición actual ya está naranja/roja y la candidata está azul.
    """

    if horizon not in HORIZON_POLICIES:
        raise ValueError(f"Horizonte no reconocido: {horizon}")
    policy = HORIZON_POLICIES[horizon]
    summary = {
        ticker: dict(row)
        for row in live_summary
        if (ticker := _ticker(row.get("Ticker")))
    }
    held = list(dict.fromkeys(_ticker(value) for value in held_tickers if _ticker(value)))
    favorites = {
        _ticker(value) for value in favorite_tickers if _ticker(value)
    }
    allocations = {
        _ticker(ticker): float(value)
        for ticker, value in (allocations_pct or {}).items()
        if _number(value) is not None
    }
    decisions = {
        _ticker(row.get("Ticker")): row
        for row in build_portfolio_decision_rows(
            summary.values(),
            held,
            allocations_pct=allocations,
            max_add_allocation_pct=max_company_weight_pct,
        )
    }

    position_rows: list[dict[str, object]] = []
    scored: dict[str, dict[str, object]] = {}
    for ticker, source in summary.items():
        scored[ticker] = horizon_score(source, horizon, today=today)
    for ticker in held:
        source = summary.get(ticker, {})
        score = scored.get(ticker) or horizon_score({}, horizon, today=today)
        decision = decisions.get(ticker, {})
        color, action = _position_color(
            str(decision.get("Decisión") or "Actualizar datos"),
            score,
            policy,
        )
        position_rows.append(
            {
                "Color": color,
                "Ticker": ticker,
                "Peso cotizado": allocations.get(ticker),
                "Score horizonte": score.get("Score horizonte"),
                "Confianza": score.get("Confianza"),
                "Cobertura": score.get("Cobertura"),
                "Acción": action,
                "Motivo": decision.get("Motivo") or "Faltan datos recientes.",
                "Entrada": source.get("Lectura entrada") or "Sin comprobar",
                "Sector": source.get("Sector") or "Sin clasificar",
                "Fecha": score.get("Fecha"),
                "Antigüedad": score.get("Antigüedad"),
            }
        )
    # En la tabla, lo que requiere revisión debe aparecer antes que una
    # posición sana, aunque el bróker haya entregado las filas en otro orden.
    position_color_priority = {
        "Rojo": 0,
        "Naranja": 1,
        "Gris": 2,
        "Amarillo": 3,
        "Azul": 4,
        "Verde": 5,
    }
    position_rows.sort(
        key=lambda row: (
            position_color_priority.get(str(row.get("Color")), 99),
            -float(row.get("Peso cotizado") or 0.0),
            str(row.get("Ticker") or ""),
        )
    )

    candidate_rows: list[dict[str, object]] = []
    for ticker in sorted(favorites.difference(held)):
        source = summary.get(ticker)
        if source is None:
            candidate_rows.append(
                {
                    "Color": "Gris",
                    "Ticker": ticker,
                    "Score horizonte": None,
                    "Confianza": 0.0,
                    "Cobertura": 0.0,
                    "Acción": "Actualizar datos antes de comparar",
                    "Entrada": "Sin comprobar",
                    "Sector": "Sin clasificar",
                    "Fecha": None,
                }
            )
            continue
        score = scored[ticker]
        color, action = _candidate_color(source, score, policy)
        candidate_rows.append(
            {
                "Color": color,
                "Ticker": ticker,
                "Score horizonte": score.get("Score horizonte"),
                "Confianza": score.get("Confianza"),
                "Cobertura": score.get("Cobertura"),
                "Acción": action,
                "Entrada": source.get("Lectura entrada") or "Sin comprobar",
                "Sector": source.get("Sector") or "Sin clasificar",
                "Fecha": score.get("Fecha"),
                "Antigüedad": score.get("Antigüedad"),
            }
        )
    candidate_rows.sort(
        key=lambda row: (
            row["Color"] != "Azul",
            -float(row.get("Score horizonte") or -1),
            str(row["Ticker"]),
        )
    )

    held_sector_weights: dict[str, float] = {}
    for ticker in held:
        sector = str(summary.get(ticker, {}).get("Sector") or "Sin clasificar")
        held_sector_weights[sector] = held_sector_weights.get(sector, 0.0) + allocations.get(ticker, 0.0)

    blue_candidates = [row for row in candidate_rows if row["Color"] == "Azul"]
    source_positions = [
        row for row in position_rows if row["Color"] in {"Naranja", "Rojo"}
    ]
    switch_rows: list[dict[str, object]] = []
    if policy.normal_rotations:
        for current in source_positions:
            current_ticker = str(current["Ticker"])
            current_score = _number(current.get("Score horizonte"))
            current_confidence = _number(current.get("Confianza")) or 0.0
            current_coverage = _number(current.get("Cobertura")) or 0.0
            current_weight = allocations.get(current_ticker, 0.0)
            if current_score is None or current_confidence < policy.minimum_confidence:
                continue
            if current_coverage < 100.0:
                continue
            # El cálculo de costes representa un cambio completo. Si el origen no
            # tiene peso fiable o supera el máximo individual, sólo procede diseñar
            # una reducción parcial manual, no fingir un salto completo.
            if current_weight <= 0 or current_weight > max_company_weight_pct:
                continue
            ranked_pairs: list[tuple[float, dict[str, object]]] = []
            current_source = summary.get(current_ticker, {})
            current_quality = _number(current_source.get("Calidad empresa"))
            current_risk = _number(current_source.get("Riesgo controlado"))
            current_sector = str(current_source.get("Sector") or "Sin clasificar")
            for candidate in blue_candidates:
                candidate_ticker = str(candidate["Ticker"])
                candidate_score = _number(candidate.get("Score horizonte"))
                candidate_confidence = _number(candidate.get("Confianza")) or 0.0
                if candidate_score is None or candidate_confidence < policy.minimum_confidence:
                    continue
                advantage = candidate_score - current_score
                if advantage < policy.minimum_advantage:
                    continue
                candidate_source = summary[candidate_ticker]
                candidate_quality = _number(candidate_source.get("Calidad empresa"))
                candidate_risk = _number(candidate_source.get("Riesgo controlado"))
                if (
                    current_quality is not None
                    and candidate_quality is not None
                    and candidate_quality < current_quality - 5
                ):
                    continue
                if (
                    current_risk is not None
                    and candidate_risk is not None
                    and candidate_risk < current_risk - 8
                ):
                    continue
                candidate_sector = str(candidate_source.get("Sector") or "Sin clasificar")
                projected_sector_weight = held_sector_weights.get(candidate_sector, 0.0)
                if candidate_sector != current_sector:
                    projected_sector_weight += current_weight
                if projected_sector_weight > max_sector_weight_pct:
                    continue
                pair = (current_ticker, candidate_ticker)
                costs = dict((recovery_hurdles or {}).get(pair, {}))
                if require_costs and not costs:
                    continue
                hurdle = _number(costs.get("Umbral recuperación"))
                if hurdle is not None and hurdle > policy.maximum_recovery_hurdle_pct:
                    continue
                correlation = _number((pair_correlations or {}).get(pair))
                if correlation is None:
                    continue
                diversification = (
                    "Mejora diversificación"
                    if candidate_sector != current_sector and correlation < 0.75
                    else "Diversificación limitada"
                )
                rank_value = advantage
                if candidate_sector != current_sector:
                    rank_value += 3.0
                if correlation is not None:
                    rank_value += 2.0 if correlation < 0.65 else -4.0 if correlation > 0.85 else 0.0
                ranked_pairs.append(
                    (
                        rank_value,
                        {
                            "Origen": current_ticker,
                            "Alternativa": candidate_ticker,
                            "Lectura": _switch_label(
                                advantage=advantage,
                                confidence=min(
                                    current_confidence, candidate_confidence
                                ),
                                hurdle=hurdle,
                                correlation=correlation,
                                policy=policy,
                            ),
                            "Ventaja score": advantage,
                            "Score origen": current_score,
                            "Score alternativa": candidate_score,
                            "Confianza mínima": min(current_confidence, candidate_confidence),
                            "Peso origen": current_weight,
                            "Sector origen": current_sector,
                            "Sector alternativa": candidate_sector,
                            "Correlación": correlation,
                            "Diversificación": diversification,
                            "Fricción económica": costs.get("Fricción económica"),
                            "Reserva fiscal": costs.get("Reserva fiscal"),
                            "Capital prudente": costs.get("Capital prudente"),
                            "Umbral recuperación": hurdle,
                            "Origen coste": costs.get("Origen coste"),
                            "Acción": "Validar tesis, resultados, precio límite y tamaño; considerar sólo un tramo.",
                        },
                    )
                )
            if ranked_pairs:
                ranked_pairs.sort(key=lambda item: item[0], reverse=True)
                switch_rows.append(ranked_pairs[0][1])
    switch_priority = {
        "Ventaja clara para estudiar": 0,
        "Cambio razonable": 1,
        "Cambio débil": 2,
    }
    switch_rows.sort(
        key=lambda row: (
            switch_priority.get(str(row.get("Lectura")), 99),
            -float(row.get("Ventaja score") or 0.0),
            float(row.get("Umbral recuperación") or float("inf")),
        )
    )
    selected_switches: list[dict[str, object]] = []
    used_destinations: set[str] = set()
    projected_sector_weights = dict(held_sector_weights)
    maximum_switches = min(
        max(0, int(limit_switches)),
        max(0, int(policy.maximum_switches)),
    )
    for row in (switch_rows if maximum_switches > 0 else []):
        destination = str(row.get("Alternativa") or "")
        if not destination or destination in used_destinations:
            continue
        origin_sector = str(row.get("Sector origen") or "Sin clasificar")
        destination_sector = str(row.get("Sector alternativa") or "Sin clasificar")
        moved_weight = float(row.get("Peso origen") or 0.0)
        if destination_sector != origin_sector:
            new_destination_weight = (
                projected_sector_weights.get(destination_sector, 0.0) + moved_weight
            )
            if new_destination_weight > max_sector_weight_pct:
                continue
            projected_sector_weights[origin_sector] = max(
                0.0,
                projected_sector_weights.get(origin_sector, 0.0) - moved_weight,
            )
            projected_sector_weights[destination_sector] = new_destination_weight
        selected_switches.append(row)
        used_destinations.add(destination)
        if len(selected_switches) >= maximum_switches:
            break

    return RotationDashboard(
        positions=tuple(position_rows),
        candidates=tuple(candidate_rows[: max(0, int(limit_candidates))]),
        switches=tuple(selected_switches),
        policy=policy,
    )


def build_rotation_priorities(
    dashboard: RotationDashboard,
    *,
    limit: int = 5,
) -> list[dict[str, object]]:
    """Resume lo accionable sin repetir el gráfico ni convertirlo en órdenes."""

    maximum = max(0, int(limit))
    if maximum == 0:
        return []
    switches_by_origin = {
        str(row.get("Origen") or ""): row
        for row in dashboard.switches
        if str(row.get("Origen") or "")
    }
    used_destinations = {
        str(row.get("Alternativa") or "")
        for row in dashboard.switches
        if str(row.get("Alternativa") or "")
    }
    position_rank = {"Rojo": 0, "Naranja": 1, "Gris": 2, "Azul": 3, "Amarillo": 4}
    labels = {
        "Rojo": "Revisar posible salida",
        "Naranja": "Revisar exposición",
        "Gris": "Actualizar datos",
        "Azul": "Estudiar ampliación",
        "Amarillo": "Vigilar",
    }
    ranked: list[tuple[int, float, dict[str, object]]] = []
    for position in dashboard.positions:
        color = str(position.get("Color") or "")
        if color not in position_rank:
            continue
        ticker = str(position.get("Ticker") or "")
        switch = switches_by_origin.get(ticker)
        alternative = str((switch or {}).get("Alternativa") or "")
        action = str(position.get("Acción") or "")
        if switch is not None:
            action = (
                f"Estudiar {ticker} → {alternative} por tramos: "
                f"{switch.get('Lectura') or 'comparación pendiente'}."
            )
        ranked.append(
            (
                position_rank[color],
                -float(position.get("Peso cotizado") or 0.0),
                {
                    "Estado": color,
                    "Prioridad": labels[color],
                    "Empresa": alternative or ticker,
                    "Posición": ticker,
                    "Alternativa": alternative or "—",
                    "Acción": action,
                    "Motivo": position.get("Motivo") or "",
                },
            )
        )
    for candidate in dashboard.candidates:
        ticker = str(candidate.get("Ticker") or "")
        if candidate.get("Color") != "Azul" or ticker in used_destinations:
            continue
        ranked.append(
            (
                3,
                -float(candidate.get("Score horizonte") or 0.0),
                {
                    "Estado": "Azul",
                    "Prioridad": "Estudiar nuevo capital",
                    "Empresa": ticker,
                    "Posición": "—",
                    "Alternativa": ticker,
                    "Acción": candidate.get("Acción") or "Validar entrada y tamaño.",
                    "Motivo": (
                        f"Score del horizonte: {float(candidate.get('Score horizonte') or 0.0):.0f}/100."
                    ),
                },
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1], str(item[2].get("Posición"))))
    return [row for _, _, row in ranked[:maximum]]
