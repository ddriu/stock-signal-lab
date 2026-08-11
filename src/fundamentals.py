"""Puntuación fundamental explicable y tolerante a datos incompletos."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable


@dataclass(frozen=True)
class FundamentalResult:
    ticker: str
    score: int | None
    coverage_pct: int
    positive_factors: tuple[str, ...]
    risk_factors: tuple[str, ...]
    country: str | None
    sector: str | None
    currency: str | None
    source_names: tuple[str, ...] = ()
    official_period_end: str | None = None
    official_url: str | None = None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _tier_score(value: float, tiers: tuple[tuple[Callable[[float], bool], float], ...]) -> float:
    for condition, points in tiers:
        if condition(value):
            return points
    return 0.0


def evaluate_fundamentals(info: dict[str, Any], ticker: str = "") -> FundamentalResult:
    """Calcula calidad 0–100 normalizando sólo sobre métricas disponibles.

    El score mide rentabilidad, crecimiento, balance y caja. No incorpora el
    precio de entrada, riesgo país, gobierno corporativo ni ventajas competitivas.
    """

    symbol = ticker or str(info.get("symbol") or "Activo")
    weighted_points = 0.0
    available_weight = 0.0
    positives: list[str] = []
    risks: list[str] = []

    def add_metric(
        key: str,
        weight: float,
        tiers: tuple[tuple[Callable[[float], bool], float], ...],
        positive_text: Callable[[float], str],
        risk_text: Callable[[float], str],
        positive_when: Callable[[float], bool],
    ) -> None:
        nonlocal weighted_points, available_weight
        value = _number(info.get(key))
        if value is None:
            return
        available_weight += weight
        weighted_points += _tier_score(value, tiers)
        if positive_when(value):
            positives.append(positive_text(value))
        else:
            risks.append(risk_text(value))

    # Rentabilidad: 35 puntos.
    add_metric(
        "returnOnEquity",
        15,
        ((lambda x: x >= 0.20, 15), (lambda x: x >= 0.12, 10), (lambda x: x > 0, 5)),
        lambda x: f"Rentabilidad sobre recursos propios sólida ({x:.1%})",
        lambda x: f"Rentabilidad sobre recursos propios débil ({x:.1%})",
        lambda x: x >= 0.12,
    )
    add_metric(
        "profitMargins",
        10,
        ((lambda x: x >= 0.20, 10), (lambda x: x >= 0.10, 7), (lambda x: x > 0, 3)),
        lambda x: f"Margen neto saludable ({x:.1%})",
        lambda x: f"Margen neto reducido o negativo ({x:.1%})",
        lambda x: x >= 0.10,
    )
    add_metric(
        "operatingMargins",
        10,
        ((lambda x: x >= 0.20, 10), (lambda x: x >= 0.10, 7), (lambda x: x > 0, 3)),
        lambda x: f"Buen margen operativo ({x:.1%})",
        lambda x: f"Margen operativo reducido o negativo ({x:.1%})",
        lambda x: x >= 0.10,
    )

    # Crecimiento: 30 puntos.
    add_metric(
        "revenueGrowth",
        15,
        ((lambda x: x >= 0.20, 15), (lambda x: x >= 0.10, 11), (lambda x: x > 0, 6)),
        lambda x: f"Los ingresos crecen ({x:.1%})",
        lambda x: f"Los ingresos se contraen ({x:.1%})",
        lambda x: x > 0,
    )
    add_metric(
        "earningsGrowth",
        15,
        ((lambda x: x >= 0.20, 15), (lambda x: x >= 0.10, 11), (lambda x: x > 0, 6)),
        lambda x: f"Los beneficios crecen ({x:.1%})",
        lambda x: f"Los beneficios no crecen ({x:.1%})",
        lambda x: x > 0,
    )

    # Balance: 25 puntos. Yahoo expresa debtToEquity habitualmente como porcentaje.
    add_metric(
        "debtToEquity",
        15,
        ((lambda x: x <= 50, 15), (lambda x: x <= 100, 10), (lambda x: x <= 200, 5)),
        lambda x: f"Endeudamiento contenido (deuda/patrimonio {x:.0f}%)",
        lambda x: f"Endeudamiento elevado (deuda/patrimonio {x:.0f}%)",
        lambda x: x <= 100,
    )
    add_metric(
        "currentRatio",
        10,
        ((lambda x: x >= 1.5, 10), (lambda x: x >= 1.0, 6), (lambda x: x >= 0.7, 3)),
        lambda x: f"Liquidez corriente cómoda ({x:.2f})",
        lambda x: f"Liquidez corriente ajustada ({x:.2f})",
        lambda x: x >= 1.0,
    )

    # Caja: 10 puntos.
    add_metric(
        "freeCashflow",
        10,
        ((lambda x: x > 0, 10),),
        lambda x: "Genera flujo de caja libre positivo",
        lambda x: "El flujo de caja libre es negativo",
        lambda x: x > 0,
    )

    coverage_pct = round(available_weight)
    # Con menos del 40% de datos el número sería engañoso.
    score = round(weighted_points / available_weight * 100) if available_weight >= 40 else None
    return FundamentalResult(
        ticker=symbol,
        score=score,
        coverage_pct=coverage_pct,
        positive_factors=tuple(positives),
        risk_factors=tuple(risks),
        country=str(info.get("country")) if info.get("country") else None,
        sector=str(info.get("sector")) if info.get("sector") else None,
        currency=str(info.get("currency")).upper() if info.get("currency") else None,
        source_names=tuple(str(value) for value in info.get("_providers", ()) if value),
        official_period_end=(
            str(info.get("_official_period_end"))
            if info.get("_official_period_end")
            else None
        ),
        official_url=str(info.get("_official_url")) if info.get("_official_url") else None,
    )
