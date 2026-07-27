"""Análisis multifactor sin convertir una única nota en una certeza."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

from src.fundamentals import FundamentalResult
from src.signal_engine import SignalResult


@dataclass(frozen=True)
class ValuationResult:
    ticker: str
    score: int | None
    coverage_pct: int
    metrics: tuple[tuple[str, float], ...]
    positive_factors: tuple[str, ...]
    risk_factors: tuple[str, ...]


@dataclass(frozen=True)
class RelativeStrengthResult:
    ticker: str
    score: int | None
    coverage_pct: int
    broad_benchmark: str | None
    sector_benchmark: str | None
    stock_return_3m_pct: float | None
    broad_excess_3m_pct: float | None
    sector_excess_3m_pct: float | None
    positive_factors: tuple[str, ...]
    risk_factors: tuple[str, ...]


@dataclass(frozen=True)
class RiskResult:
    ticker: str
    score: int | None
    coverage_pct: int
    annualized_volatility_pct: float | None
    max_drawdown_1y_pct: float | None
    average_turnover_20d: float | None
    atr_pct: float | None
    positive_factors: tuple[str, ...]
    risk_factors: tuple[str, ...]


@dataclass(frozen=True)
class OpportunityResult:
    ticker: str
    score: int
    confidence_pct: int
    label: str
    explanation: str
    positive_factors: tuple[str, ...]
    risk_factors: tuple[str, ...]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _band(value: float, bands: tuple[tuple[Callable[[float], bool], int], ...]) -> int:
    for condition, points in bands:
        if condition(value):
            return points
    return 0


def evaluate_valuation(info: dict[str, Any], ticker: str = "") -> ValuationResult:
    """Puntúa valoración con PER, PEG, precio/valor contable y caja.

    Es una aproximación generalista. Los múltiplos deben interpretarse dentro
    del sector; por eso se muestran las métricas y la cobertura junto a la nota.
    """

    symbol = ticker or str(info.get("symbol") or "Activo")
    weighted_score = 0.0
    available_weight = 0.0
    metrics: list[tuple[str, float]] = []
    positives: list[str] = []
    risks: list[str] = []

    def add(
        name: str,
        value: float | None,
        weight: float,
        scorer: Callable[[float], int],
        positive: Callable[[float], bool],
        positive_text: Callable[[float], str],
        risk_text: Callable[[float], str],
    ) -> None:
        nonlocal weighted_score, available_weight
        if value is None:
            return
        available_weight += weight
        weighted_score += weight * scorer(value) / 100.0
        metrics.append((name, value))
        if positive(value):
            positives.append(positive_text(value))
        else:
            risks.append(risk_text(value))

    forward_pe = _number(info.get("forwardPE"))
    if forward_pe is not None and forward_pe <= 0:
        forward_pe = None
    trailing_pe = _number(info.get("trailingPE"))
    if trailing_pe is not None and trailing_pe <= 0:
        trailing_pe = None
    peg = _number(info.get("pegRatio"))
    if peg is not None and peg <= 0:
        peg = None
    price_to_book = _number(info.get("priceToBook"))
    if price_to_book is not None and price_to_book <= 0:
        price_to_book = None
    market_cap = _number(info.get("marketCap"))
    free_cash_flow = _number(info.get("freeCashflow"))
    fcf_yield = (
        free_cash_flow / market_cap * 100.0
        if market_cap not in (None, 0) and free_cash_flow is not None
        else None
    )

    pe_score = lambda value: _band(
        value,
        (
            (lambda x: x <= 12, 95),
            (lambda x: x <= 20, 80),
            (lambda x: x <= 30, 60),
            (lambda x: x <= 45, 40),
            (lambda x: True, 20),
        ),
    )
    add(
        "PER futuro",
        forward_pe,
        30,
        pe_score,
        lambda x: x <= 25,
        lambda x: f"PER futuro moderado ({x:.1f} veces beneficio)",
        lambda x: f"PER futuro exigente ({x:.1f} veces beneficio)",
    )
    add(
        "PER histórico",
        trailing_pe,
        15,
        pe_score,
        lambda x: x <= 25,
        lambda x: f"PER histórico moderado ({x:.1f})",
        lambda x: f"PER histórico elevado ({x:.1f})",
    )
    add(
        "PEG",
        peg,
        25,
        lambda value: _band(
            value,
            (
                (lambda x: x <= 1.0, 95),
                (lambda x: x <= 1.5, 80),
                (lambda x: x <= 2.5, 60),
                (lambda x: x <= 4.0, 35),
                (lambda x: True, 15),
            ),
        ),
        lambda x: x <= 1.5,
        lambda x: f"El precio parece razonable frente al crecimiento esperado (PEG {x:.2f})",
        lambda x: f"El precio exige bastante crecimiento futuro (PEG {x:.2f})",
    )
    add(
        "Flujo de caja libre / capitalización (%)",
        fcf_yield,
        20,
        lambda value: _band(
            value,
            (
                (lambda x: x >= 8, 100),
                (lambda x: x >= 5, 80),
                (lambda x: x >= 3, 60),
                (lambda x: x > 0, 35),
                (lambda x: True, 5),
            ),
        ),
        lambda x: x >= 5,
        lambda x: f"Buena generación de caja frente al precio ({x:.1f}%)",
        lambda x: f"Generación de caja reducida frente al precio ({x:.1f}%)",
    )
    add(
        "Precio / valor contable",
        price_to_book,
        10,
        lambda value: _band(
            value,
            (
                (lambda x: x <= 1.5, 90),
                (lambda x: x <= 3, 70),
                (lambda x: x <= 6, 50),
                (lambda x: True, 25),
            ),
        ),
        lambda x: x <= 3,
        lambda x: f"Precio/valor contable contenido ({x:.1f})",
        lambda x: f"Precio/valor contable alto ({x:.1f}); depende mucho del sector",
    )

    coverage = round(available_weight)
    score = round(weighted_score / available_weight * 100) if available_weight >= 25 else None
    return ValuationResult(
        ticker=symbol,
        score=score,
        coverage_pct=coverage,
        metrics=tuple(metrics),
        positive_factors=tuple(positives),
        risk_factors=tuple(risks),
    )


def _period_return(frame: pd.DataFrame | None, sessions: int) -> float | None:
    if frame is None or frame.empty or "close" not in frame:
        return None
    close = pd.to_numeric(frame["close"], errors="coerce").dropna()
    if len(close) <= sessions:
        return None
    initial = float(close.iloc[-sessions - 1])
    if initial == 0:
        return None
    return (float(close.iloc[-1]) / initial - 1.0) * 100.0


def evaluate_relative_strength(
    ticker: str,
    stock: pd.DataFrame,
    broad: pd.DataFrame | None,
    *,
    broad_name: str | None,
    sector: pd.DataFrame | None = None,
    sector_name: str | None = None,
) -> RelativeStrengthResult:
    """Mide si la acción avanza más o menos que su mercado y su sector."""

    components = (
        (63, broad, 25),
        (126, broad, 20),
        (252, broad, 25),
        (63, sector, 15),
        (126, sector, 15),
    )
    weighted = 0.0
    available = 0.0
    for sessions, reference, weight in components:
        stock_return = _period_return(stock, sessions)
        reference_return = _period_return(reference, sessions)
        if stock_return is None or reference_return is None:
            continue
        excess = stock_return - reference_return
        component_score = float(np.clip(50.0 + excess * 2.0, 0.0, 100.0))
        weighted += weight * component_score / 100.0
        available += weight
    score = round(weighted / available * 100) if available >= 25 else None
    stock_3m = _period_return(stock, 63)
    broad_3m = _period_return(broad, 63)
    sector_3m = _period_return(sector, 63)
    broad_excess = (
        stock_3m - broad_3m
        if stock_3m is not None and broad_3m is not None
        else None
    )
    sector_excess = (
        stock_3m - sector_3m
        if stock_3m is not None and sector_3m is not None
        else None
    )
    positives: list[str] = []
    risks: list[str] = []
    if broad_excess is not None:
        destination = positives if broad_excess >= 0 else risks
        destination.append(
            f"En tres meses supera al mercado en {broad_excess:+.1f} puntos porcentuales"
            if broad_excess >= 0
            else f"En tres meses queda por detrás del mercado en {abs(broad_excess):.1f} puntos"
        )
    if sector_excess is not None:
        destination = positives if sector_excess >= 0 else risks
        destination.append(
            f"También supera a su sector en {sector_excess:+.1f} puntos"
            if sector_excess >= 0
            else f"Va por detrás de su sector en {abs(sector_excess):.1f} puntos"
        )
    return RelativeStrengthResult(
        ticker=ticker,
        score=score,
        coverage_pct=round(available),
        broad_benchmark=broad_name if broad is not None else None,
        sector_benchmark=sector_name if sector is not None else None,
        stock_return_3m_pct=stock_3m,
        broad_excess_3m_pct=broad_excess,
        sector_excess_3m_pct=sector_excess,
        positive_factors=tuple(positives),
        risk_factors=tuple(risks),
    )


def evaluate_risk(ticker: str, frame: pd.DataFrame) -> RiskResult:
    """Resume volatilidad, caída, liquidez y amplitud diaria; 100 es más estable."""

    close = pd.to_numeric(frame.get("close"), errors="coerce").dropna()
    returns = close.pct_change(fill_method=None).dropna().tail(60)
    volatility = (
        float(returns.std() * np.sqrt(252) * 100)
        if len(returns) >= 20
        else None
    )
    one_year = close.tail(252)
    drawdown = (
        float((one_year / one_year.cummax() - 1.0).min() * 100)
        if len(one_year) >= 60
        else None
    )
    turnover = None
    if "volume" in frame and len(frame) >= 20:
        turnover_series = (
            pd.to_numeric(frame["volume"], errors="coerce")
            * pd.to_numeric(frame["close"], errors="coerce")
        )
        turnover = float(turnover_series.tail(20).mean())
    atr_pct = None
    if "atr_14" in frame and not frame["atr_14"].dropna().empty and not close.empty:
        atr_pct = float(frame["atr_14"].dropna().iloc[-1] / close.iloc[-1] * 100)

    values: list[tuple[float, float]] = []
    if volatility is not None:
        values.append(
            (
                35,
                _band(
                    volatility,
                    (
                        (lambda x: x <= 20, 95),
                        (lambda x: x <= 30, 80),
                        (lambda x: x <= 45, 60),
                        (lambda x: x <= 65, 35),
                        (lambda x: True, 15),
                    ),
                ),
            )
        )
    if drawdown is not None:
        values.append(
            (
                35,
                _band(
                    drawdown,
                    (
                        (lambda x: x >= -10, 95),
                        (lambda x: x >= -20, 75),
                        (lambda x: x >= -35, 50),
                        (lambda x: x >= -50, 30),
                        (lambda x: True, 10),
                    ),
                ),
            )
        )
    if turnover is not None and np.isfinite(turnover):
        values.append(
            (
                20,
                _band(
                    turnover,
                    (
                        (lambda x: x >= 100_000_000, 100),
                        (lambda x: x >= 20_000_000, 85),
                        (lambda x: x >= 5_000_000, 65),
                        (lambda x: x >= 1_000_000, 40),
                        (lambda x: True, 15),
                    ),
                ),
            )
        )
    if atr_pct is not None:
        values.append(
            (
                10,
                _band(
                    atr_pct,
                    (
                        (lambda x: x <= 2, 90),
                        (lambda x: x <= 3.5, 70),
                        (lambda x: x <= 5, 50),
                        (lambda x: x <= 8, 30),
                        (lambda x: True, 10),
                    ),
                ),
            )
        )
    available = sum(weight for weight, _ in values)
    score = (
        round(sum(weight * points / 100 for weight, points in values) / available * 100)
        if available >= 35
        else None
    )
    positives: list[str] = []
    risks: list[str] = []
    if volatility is not None:
        (positives if volatility <= 30 else risks).append(
            f"Volatilidad anualizada {'contenida' if volatility <= 30 else 'elevada'} ({volatility:.1f}%)"
        )
    if drawdown is not None:
        (positives if drawdown >= -20 else risks).append(
            f"Peor caída del último año: {drawdown:.1f}%"
        )
    if turnover is not None and turnover < 1_000_000:
        risks.append("Negociación monetaria reducida; una orden puede mover más el precio")
    return RiskResult(
        ticker=ticker,
        score=score,
        coverage_pct=round(available),
        annualized_volatility_pct=volatility,
        max_drawdown_1y_pct=drawdown,
        average_turnover_20d=turnover,
        atr_pct=atr_pct,
        positive_factors=tuple(positives),
        risk_factors=tuple(risks),
    )


def combine_opportunity(
    ticker: str,
    fundamentals: FundamentalResult,
    valuation: ValuationResult,
    signal: SignalResult,
    relative: RelativeStrengthResult,
    risk: RiskResult,
) -> OpportunityResult:
    """Combina familias independientes y conserva sus notas por separado."""

    components = (
        ("calidad", fundamentals.score, 30.0, fundamentals.coverage_pct),
        ("valoración", valuation.score, 15.0, valuation.coverage_pct),
        ("momento", signal.score, 25.0, 100),
        ("fortaleza relativa", relative.score, 15.0, relative.coverage_pct),
        ("riesgo", risk.score, 15.0, risk.coverage_pct),
    )
    available = [(name, score, weight) for name, score, weight, _ in components if score is not None]
    total_weight = sum(weight for _, _, weight in available)
    score = round(
        sum(float(component_score) * weight for _, component_score, weight in available)
        / total_weight
    )
    confidence = round(
        sum(weight * coverage / 100.0 for _, _, weight, coverage in components)
    )

    quality_ok = fundamentals.score is None or fundamentals.score >= 55
    risk_ok = risk.score is None or risk.score >= 35
    entry_ready = signal.label in {"Entrada fuerte", "Entrada interesante"}
    if confidence < 45:
        label = "Datos insuficientes"
    elif signal.label == "Esperar" and signal.score >= 65:
        label = "Esperar mejor precio"
    elif score >= 75 and entry_ready and quality_ok and risk_ok:
        label = "Oportunidad destacada"
    elif score >= 65 and entry_ready and quality_ok and risk_ok:
        label = "Candidata"
    elif score >= 55 or signal.label == "Vigilancia":
        label = "Vigilancia"
    else:
        label = "Esperar"

    positives: list[str] = []
    risks: list[str] = []
    if fundamentals.score is not None:
        (positives if fundamentals.score >= 65 else risks).append(
            f"Calidad empresarial {fundamentals.score}/100"
        )
    else:
        risks.append("No hay suficientes datos para puntuar la calidad empresarial")
    if valuation.score is not None:
        (positives if valuation.score >= 60 else risks).append(
            f"Valoración {valuation.score}/100"
        )
    else:
        risks.append("La valoración tiene datos insuficientes")
    if relative.score is not None:
        (positives if relative.score >= 55 else risks).append(
            f"Fortaleza frente al mercado {relative.score}/100"
        )
    if risk.score is not None:
        (positives if risk.score >= 55 else risks).append(
            f"Control de riesgo {risk.score}/100"
        )
    if entry_ready:
        positives.append(f"El momento técnico permite estudiar una entrada ({signal.score}/100)")
    else:
        risks.append(f"El momento técnico todavía se clasifica como «{signal.label}»")

    explanation = (
        f"{ticker} obtiene {score}/100 en oportunidad conjunta, con una confianza de datos "
        f"del {confidence}%. La lectura es «{label}». "
        "Las notas de empresa, valoración, momento, liderazgo y riesgo permanecen separadas "
        "para que una fortaleza no oculte una debilidad."
    )
    return OpportunityResult(
        ticker=ticker,
        score=score,
        confidence_pct=confidence,
        label=label,
        explanation=explanation,
        positive_factors=tuple(positives),
        risk_factors=tuple(risks),
    )
