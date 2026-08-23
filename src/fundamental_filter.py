"""Filtro fundamental rápido, explicable y consciente del sector.

El filtro traduce una lista popular de siete comprobaciones contables a una
lectura útil para la aplicación. No es una señal de compra: separa calidad y
valoración del momento técnico y hace visible cuándo faltan datos.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class FundamentalCheck:
    key: str
    label: str
    value: float | None
    formatted_value: str
    rule: str
    status: str
    points: float
    weight: float
    explanation: str


@dataclass(frozen=True)
class FundamentalFilterResult:
    ticker: str
    score: int | None
    coverage_pct: int
    passed: int
    evaluated: int
    label: str
    sector: str
    checks: tuple[FundamentalCheck, ...]
    warning: str


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _sector_group(sector: str) -> str:
    value = sector.casefold()
    if any(word in value for word in ("technology", "communication", "health")):
        return "growth"
    if any(word in value for word in ("financial", "bank", "insurance")):
        return "financial"
    if any(word in value for word in ("utility", "real estate", "reit")):
        return "capital_intensive"
    if any(word in value for word in ("energy", "basic materials")):
        return "cyclical"
    return "general"


def _format_multiple(value: float | None) -> str:
    return "N/D" if value is None else f"{value:.1f}x"


def _format_pct(value: float | None) -> str:
    return "N/D" if value is None else f"{value:.1%}"


def _format_debt(value: float | None) -> str:
    return "N/D" if value is None else f"{value / 100:.2f}x"


def evaluate_fundamental_filter(
    info: dict[str, Any],
    ticker: str = "",
) -> FundamentalFilterResult:
    """Evalúa siete filtros sin penalizar las métricas ausentes.

    Yahoo expresa ``debtToEquity`` como porcentaje. ``earningsGrowth`` se usa
    como aproximación cuando no existe un CAGR de BPA de varios ejercicios; la
    interfaz lo declara expresamente para no presentar una precisión falsa.
    """

    symbol = (ticker or str(info.get("symbol") or "Activo")).strip().upper()
    sector = str(info.get("sector") or "Sector no identificado").strip()
    group = _sector_group(sector)
    pe_limit = 30.0 if group == "growth" else 25.0 if group == "capital_intensive" else 20.0
    gross_limit = 0.30 if group in {"cyclical", "capital_intensive"} else 0.40

    pe = _number(info.get("forwardPE")) or _number(info.get("trailingPE"))
    roic = _number(info.get("returnOnInvestedCapital"))
    debt_to_equity = _number(info.get("debtToEquity"))
    eps_growth = _number(info.get("epsGrowthCagr"))
    eps_is_proxy = eps_growth is None
    if eps_growth is None:
        eps_growth = _number(info.get("earningsGrowth"))
    roe = _number(info.get("returnOnEquity"))
    ebit_margin = _number(info.get("operatingMargins"))
    gross_margin = _number(info.get("grossMargins"))

    checks: list[FundamentalCheck] = []

    def add(
        key: str,
        label: str,
        value: float | None,
        *,
        rule: str,
        passes: bool | None,
        weight: float,
        formatted: str,
        explanation: str,
        not_applicable: bool = False,
    ) -> None:
        if not_applicable:
            status = "No comparable"
            points = 0.0
        elif value is None:
            status = "Sin datos"
            points = 0.0
        elif passes:
            status = "Cumple"
            points = weight
        else:
            status = "No cumple"
            points = 0.0
        checks.append(
            FundamentalCheck(
                key=key,
                label=label,
                value=value,
                formatted_value=formatted,
                rule=rule,
                status=status,
                points=points,
                weight=weight,
                explanation=explanation,
            )
        )

    add(
        "pe",
        "PER",
        pe,
        rule=f"≤ {pe_limit:.0f}x para {sector}",
        passes=pe is not None and 0 < pe <= pe_limit,
        weight=15,
        formatted=_format_multiple(pe),
        explanation="Se compara con un límite sectorial; un PER bajo no compensa un negocio en deterioro.",
    )
    add(
        "roic",
        "ROIC",
        roic,
        rule="> 10%",
        passes=roic is not None and roic > 0.10,
        weight=20,
        formatted=_format_pct(roic),
        explanation="Mide la rentabilidad del capital operativo. Es más útil cuando se mantiene varios años.",
        not_applicable=group == "financial",
    )
    add(
        "debt_to_equity",
        "Deuda / patrimonio",
        debt_to_equity,
        rule="< 1,0x",
        passes=debt_to_equity is not None and debt_to_equity < 100,
        weight=10,
        formatted=_format_debt(debt_to_equity),
        explanation="En bancos, aseguradoras, REIT y utilities la deuda exige métricas específicas.",
        not_applicable=group in {"financial", "capital_intensive"},
    )
    growth_label = "CAGR BPA" if not eps_is_proxy else "Crecimiento beneficio (proxy BPA)"
    add(
        "eps_growth",
        growth_label,
        eps_growth,
        rule="> 8%",
        passes=eps_growth is not None and eps_growth > 0.08,
        weight=20,
        formatted=_format_pct(eps_growth),
        explanation=(
            "Se usa el crecimiento disponible como aproximación; no siempre representa un CAGR de 3–5 años."
            if eps_is_proxy
            else "Crecimiento anual compuesto del beneficio por acción."
        ),
    )
    add(
        "roe",
        "ROE",
        roe,
        rule="> 15%",
        passes=roe is not None and roe > 0.15,
        weight=10,
        formatted=_format_pct(roe),
        explanation="Puede inflarse por deuda, recompras o patrimonio muy pequeño; se contrasta con ROIC.",
    )
    add(
        "ebit_margin",
        "Margen operativo",
        ebit_margin,
        rule="> 10%",
        passes=ebit_margin is not None and ebit_margin > 0.10,
        weight=15,
        formatted=_format_pct(ebit_margin),
        explanation="Importan el nivel, la estabilidad y la dirección del margen frente a competidores.",
    )
    add(
        "gross_margin",
        "Margen bruto",
        gross_margin,
        rule=f"> {gross_limit:.0%} para {sector}",
        passes=gross_margin is not None and gross_margin > gross_limit,
        weight=10,
        formatted=_format_pct(gross_margin),
        explanation="El umbral se rebaja en negocios intensivos y cíclicos; debe compararse con su sector.",
        not_applicable=group == "financial",
    )

    applicable = [check for check in checks if check.status != "No comparable"]
    available = [check for check in applicable if check.status != "Sin datos"]
    available_weight = sum(check.weight for check in available)
    applicable_weight = sum(check.weight for check in applicable)
    points = sum(check.points for check in available)
    coverage = round(available_weight / applicable_weight * 100) if applicable_weight else 0
    score = round(points / available_weight * 100) if available_weight >= 35 else None
    passed = sum(check.status == "Cumple" for check in available)

    if score is None:
        label = "Datos insuficientes"
    elif score >= 80:
        label = "Fundamentos sólidos"
    elif score >= 65:
        label = "Interesante para estudiar"
    elif score >= 50:
        label = "Calidad mixta"
    else:
        label = "Fundamentos frágiles"

    warning = ""
    if coverage < 70:
        warning = "Cobertura limitada: faltan métricas y la nota debe interpretarse con cautela."
    elif eps_is_proxy:
        warning = "El crecimiento disponible es interanual; aún no equivale a un CAGR de BPA validado."

    return FundamentalFilterResult(
        ticker=symbol,
        score=score,
        coverage_pct=coverage,
        passed=passed,
        evaluated=len(available),
        label=label,
        sector=sector,
        checks=tuple(checks),
        warning=warning,
    )

