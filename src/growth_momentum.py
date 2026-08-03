"""Motor independiente para aportaciones mensuales de crecimiento y momentum.

La estrategia conserva tres notas separadas: crecimiento empresarial, fortaleza
del precio y contexto de mercado/riesgo. Los ajustes sectoriales sólo modifican
el dimensionamiento y las comprobaciones relevantes; no prometen rentabilidad.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.opportunity import RelativeStrengthResult, RiskResult


@dataclass(frozen=True)
class GrowthMomentumConfig:
    """Política común de capital y umbrales de la estrategia dinámica."""

    monthly_allocation_pct: float = 20.0
    strategy_cap_pct: float = 15.0
    normal_risk_pct: float = 0.50
    max_open_risk_pct: float = 2.0
    max_sector_pct: float = 20.0
    min_turnover_eur: float = 5_000_000.0
    watch_score: int = 65
    candidate_score: int = 75
    strong_score: int = 82
    commission_per_order_eur: float = 1.0
    min_stop_pct: float = 5.0
    max_stop_pct: float = 25.0

    def validate(self) -> None:
        percentages = (
            self.monthly_allocation_pct,
            self.strategy_cap_pct,
            self.normal_risk_pct,
            self.max_open_risk_pct,
            self.max_sector_pct,
        )
        if any(value <= 0 or value > 100 for value in percentages):
            raise ValueError("Los porcentajes de capital y riesgo deben estar entre 0 y 100.")
        if self.normal_risk_pct > self.max_open_risk_pct:
            raise ValueError("El riesgo por operación no puede superar el riesgo abierto total.")
        if not 0 <= self.watch_score < self.candidate_score < self.strong_score <= 100:
            raise ValueError("Los niveles deben cumplir: vigilancia < candidata < fuerte.")
        if self.min_turnover_eur < 0 or self.commission_per_order_eur < 0:
            raise ValueError("Liquidez y comisiones no pueden ser negativas.")
        if not 0 < self.min_stop_pct < self.max_stop_pct < 100:
            raise ValueError("El rango de stop debe ser positivo y creciente.")


@dataclass(frozen=True)
class SectorProfile:
    """Ajustes de riesgo y preguntas específicas de una familia empresarial."""

    key: str
    label: str
    description: str
    risk_multiplier: float
    atr_multiplier: float
    max_position_pct: float
    preferred_revenue_growth_pct: float | None
    preferred_operating_margin_pct: float | None
    manual_checks: tuple[str, ...]


SECTOR_PROFILES: dict[str, SectorProfile] = {
    "general": SectorProfile(
        key="general",
        label="General",
        description="Empresa sin un perfil sectorial suficientemente claro.",
        risk_multiplier=0.85,
        atr_multiplier=2.4,
        max_position_pct=6.0,
        preferred_revenue_growth_pct=10.0,
        preferred_operating_margin_pct=10.0,
        manual_checks=(
            "Confirmar el catalizador que podría sostener el crecimiento.",
            "Revisar deuda, generación de caja y posibles ampliaciones.",
        ),
    ),
    "technology": SectorProfile(
        key="technology",
        label="Tecnología y software",
        description="Premia crecimiento recurrente, márgenes y caja; tolera valoraciones superiores.",
        risk_multiplier=1.0,
        atr_multiplier=2.3,
        max_position_pct=8.0,
        preferred_revenue_growth_pct=15.0,
        preferred_operating_margin_pct=15.0,
        manual_checks=(
            "Comprobar si el crecimiento de ventas se acelera o desacelera.",
            "Revisar margen bruto, flujo de caja y dependencia de pocos clientes.",
            "Comparar la valoración con empresas de crecimiento parecido.",
        ),
    ),
    "consumer": SectorProfile(
        key="consumer",
        label="Consumo y marcas",
        description="Da más importancia a ventas, inventarios, márgenes y poder de fijación de precios.",
        risk_multiplier=0.9,
        atr_multiplier=2.2,
        max_position_pct=7.0,
        preferred_revenue_growth_pct=6.0,
        preferred_operating_margin_pct=10.0,
        manual_checks=(
            "Revisar ventas comparables, inventarios y evolución de márgenes.",
            "Comprobar si la marca mantiene demanda sin abusar de descuentos.",
            "Medir el impacto de divisas y de la orientación anual de la empresa.",
        ),
    ),
    "energy": SectorProfile(
        key="energy",
        label="Energía, utilities y uranio",
        description="Sector cíclico y regulado; reduce tamaño y admite más volatilidad normal.",
        risk_multiplier=0.75,
        atr_multiplier=2.8,
        max_position_pct=5.0,
        preferred_revenue_growth_pct=5.0,
        preferred_operating_margin_pct=10.0,
        manual_checks=(
            "Revisar precio de la materia prima, contratos y coste de producción.",
            "Comprobar riesgo regulatorio, geográfico y de ejecución de proyectos.",
            "Sumar toda la exposición correlacionada a nuclear, uranio y electricidad.",
        ),
    ),
    "biotech": SectorProfile(
        key="biotech",
        label="Biotecnología y salud binaria",
        description="Los ensayos, autorizaciones y ampliaciones pueden provocar saltos que un stop no evita.",
        risk_multiplier=0.5,
        atr_multiplier=3.0,
        max_position_pct=3.0,
        preferred_revenue_growth_pct=None,
        preferred_operating_margin_pct=None,
        manual_checks=(
            "Anotar fase clínica, fecha del próximo catalizador y probabilidad de retraso.",
            "Calcular meses de caja y riesgo de ampliación de capital.",
            "Asumir que un resultado adverso puede saltar el stop y causar una pérdida mayor.",
        ),
    ),
    "industrial": SectorProfile(
        key="industrial",
        label="Industria y defensa",
        description="El crecimiento debe estar respaldado por pedidos, contratos y márgenes ejecutables.",
        risk_multiplier=0.9,
        atr_multiplier=2.4,
        max_position_pct=6.0,
        preferred_revenue_growth_pct=8.0,
        preferred_operating_margin_pct=10.0,
        manual_checks=(
            "Revisar cartera de pedidos y relación entre pedidos nuevos y ventas.",
            "Comprobar concentración en gobiernos, clientes o grandes contratos.",
            "Vigilar costes, retrasos y conversión de pedidos en flujo de caja.",
        ),
    ),
    "financial": SectorProfile(
        key="financial",
        label="Banca y servicios financieros",
        description="Sustituye métricas industriales por rentabilidad, crédito y solvencia.",
        risk_multiplier=0.85,
        atr_multiplier=2.3,
        max_position_pct=6.0,
        preferred_revenue_growth_pct=None,
        preferred_operating_margin_pct=None,
        manual_checks=(
            "Revisar ROE, morosidad, coste del crédito y capital regulatorio.",
            "Comprobar sensibilidad a tipos de interés y calidad de depósitos.",
            "Comparar precio/valor contable con entidades de riesgo parecido.",
        ),
    ),
    "etf": SectorProfile(
        key="etf",
        label="ETF y fondo cotizado",
        description="No puntúa como empresa; se centra en tendencia, liquidez, costes y concentración.",
        risk_multiplier=1.0,
        atr_multiplier=2.0,
        max_position_pct=10.0,
        preferred_revenue_growth_pct=None,
        preferred_operating_margin_pct=None,
        manual_checks=(
            "Revisar TER, diferencial de compra/venta y calidad de réplica.",
            "Abrir la cartera del fondo para detectar solapamientos.",
            "Comprobar concentración sectorial, geográfica y por empresa.",
        ),
    ),
}


@dataclass(frozen=True)
class GrowthMomentumResult:
    ticker: str
    as_of: pd.Timestamp
    score: int
    confidence_pct: int
    label: str
    growth_score: int | None
    momentum_score: int
    context_score: int
    sector_key: str
    sector_label: str
    is_small_cap: bool
    price: float
    suggested_risk_pct: float
    atr_stop_pct: float
    max_position_pct: float
    positive_factors: tuple[str, ...]
    risk_factors: tuple[str, ...]
    manual_checks: tuple[str, ...]


@dataclass(frozen=True)
class GrowthPositionPlan:
    monthly_strategy_budget: float
    remaining_strategy_capacity: float
    remaining_sector_capacity: float
    remaining_open_risk: float
    risk_budget: float
    suggested_position_value: float
    quantity: float
    stop_distance_pct: float
    stop_price: float
    reference_target_2r: float
    loss_at_stop: float
    round_trip_commission: float
    commission_drag_pct: float


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _score_higher(value: float, *, excellent: float, good: float, acceptable: float) -> int:
    if value >= excellent:
        return 100
    if value >= good:
        return 80
    if value >= acceptable:
        return 60
    if value > 0:
        return 35
    return 10


def _weighted_score(parts: list[tuple[float, int]]) -> int | None:
    total = sum(weight for weight, _ in parts)
    if total <= 0:
        return None
    return round(sum(weight * score for weight, score in parts) / total)


def classify_sector_profile(info: dict[str, Any], ticker: str = "") -> SectorProfile:
    """Clasifica con campos públicos; la interfaz permite corregir el resultado."""

    text = " ".join(
        str(value or "").lower()
        for value in (
            info.get("quoteType"),
            info.get("sector"),
            info.get("industry"),
            info.get("shortName"),
            info.get("longName"),
            ticker,
        )
    )
    quote_type = str(info.get("quoteType") or "").strip().upper()
    if quote_type in {"ETF", "MUTUALFUND"} or any(
        term in text for term in (" etf", "fund", "ucits", "physical gold", "index")
    ):
        return SECTOR_PROFILES["etf"]
    if any(term in text for term in ("biotech", "therapeut", "pharma", "drug", "life science")):
        return SECTOR_PROFILES["biotech"]
    if any(
        term in text
        for term in (
            "energy",
            "uranium",
            "nuclear",
            "oil",
            "gas",
            "utilities",
            "electricity",
            "solar",
        )
    ):
        return SECTOR_PROFILES["energy"]
    if any(term in text for term in ("technology", "software", "semiconductor", "internet")):
        return SECTOR_PROFILES["technology"]
    if any(term in text for term in ("financial", "bank", "insurance", "credit services")):
        return SECTOR_PROFILES["financial"]
    if any(term in text for term in ("consumer", "retail", "apparel", "restaurant", "entertainment")):
        return SECTOR_PROFILES["consumer"]
    if any(term in text for term in ("industrial", "aerospace", "defense", "machinery")):
        return SECTOR_PROFILES["industrial"]
    return SECTOR_PROFILES["general"]


def _growth_quality(
    info: dict[str, Any],
    profile: SectorProfile,
) -> tuple[int | None, int, list[str], list[str]]:
    if profile.key == "etf":
        return None, 0, [], ["Un ETF no se evalúa con beneficios y márgenes empresariales"]

    parts: list[tuple[float, int]] = []
    positives: list[str] = []
    risks: list[str] = []
    available_weight = 0.0

    def add_ratio(
        key: str,
        weight: float,
        label: str,
        excellent: float,
        good: float,
        acceptable: float,
    ) -> None:
        nonlocal available_weight
        value = _number(info.get(key))
        if value is None:
            return
        percentage = value * 100.0
        points = _score_higher(
            percentage,
            excellent=excellent,
            good=good,
            acceptable=acceptable,
        )
        parts.append((weight, points))
        available_weight += weight
        destination = positives if points >= 60 else risks
        destination.append(f"{label}: {percentage:+.1f}%")

    if profile.key == "financial":
        add_ratio("earningsGrowth", 35, "Crecimiento del beneficio", 25, 15, 5)
        add_ratio("returnOnEquity", 35, "Rentabilidad sobre recursos propios", 20, 14, 8)
        add_ratio("revenueGrowth", 20, "Crecimiento de ingresos", 15, 8, 3)
        add_ratio("profitMargins", 10, "Margen neto", 25, 15, 5)
    elif profile.key == "biotech":
        # Estas cifras no capturan ensayos ni caja disponible. Se mantiene una
        # cobertura deliberadamente baja para exigir la comprobación manual.
        add_ratio("revenueGrowth", 20, "Crecimiento de ingresos", 30, 15, 5)
        add_ratio("earningsGrowth", 10, "Crecimiento del beneficio", 30, 15, 5)
        free_cash_flow = _number(info.get("freeCashflow"))
        if free_cash_flow is not None:
            available_weight += 10
            points = 80 if free_cash_flow > 0 else 10
            parts.append((10, points))
            (positives if free_cash_flow > 0 else risks).append(
                "Flujo de caja libre positivo" if free_cash_flow > 0 else "Consume caja; hay que estimar su autonomía"
            )
    else:
        revenue_target = profile.preferred_revenue_growth_pct or 10.0
        margin_target = profile.preferred_operating_margin_pct or 10.0
        add_ratio(
            "revenueGrowth",
            30,
            "Crecimiento de ingresos",
            max(revenue_target * 1.7, revenue_target + 10),
            revenue_target,
            max(revenue_target * 0.5, 3),
        )
        add_ratio("earningsGrowth", 25, "Crecimiento del beneficio", 25, 15, 5)
        add_ratio(
            "operatingMargins",
            15,
            "Margen operativo",
            max(margin_target * 1.7, margin_target + 10),
            margin_target,
            max(margin_target * 0.5, 3),
        )
        add_ratio("returnOnEquity", 15, "Rentabilidad sobre recursos propios", 20, 12, 5)
        free_cash_flow = _number(info.get("freeCashflow"))
        if free_cash_flow is not None:
            available_weight += 15
            points = 85 if free_cash_flow > 0 else 10
            parts.append((15, points))
            (positives if free_cash_flow > 0 else risks).append(
                "Genera flujo de caja libre" if free_cash_flow > 0 else "El flujo de caja libre es negativo"
            )

    coverage = min(100, round(available_weight))
    return _weighted_score(parts), coverage, positives, risks


def _momentum_score(
    frame: pd.DataFrame,
    relative: RelativeStrengthResult | None,
) -> tuple[int, list[str], list[str], bool]:
    valid = frame.dropna(subset=["close", "sma_medium", "sma_long"])
    if valid.empty:
        raise ValueError("No hay histórico suficiente para el perfil de momentum.")
    row = valid.iloc[-1]
    close = float(row["close"])
    parts: list[tuple[float, int]] = []
    positives: list[str] = []
    risks: list[str] = []

    above_medium = close > float(row["sma_medium"])
    above_long = close > float(row["sma_long"])
    medium_rising = float(row.get("sma_medium_slope", 0.0)) > 0
    for weight, condition, positive, risk in (
        (12, above_medium, "Cotiza por encima de la media intermedia", "Está por debajo de la media intermedia"),
        (14, above_long, "Conserva la tendencia por encima de la media larga", "Está por debajo de la media larga"),
        (8, medium_rising, "La tendencia intermedia asciende", "La tendencia intermedia no asciende"),
    ):
        parts.append((weight, 100 if condition else 5))
        (positives if condition else risks).append(positive if condition else risk)

    momentum_short = _number(row.get("momentum_short_pct"))
    if momentum_short is not None:
        points = _score_higher(momentum_short, excellent=10, good=4, acceptable=0)
        parts.append((10, points))
    momentum_medium = _number(row.get("momentum_medium_pct"))
    if momentum_medium is not None:
        points = _score_higher(momentum_medium, excellent=25, good=10, acceptable=0)
        parts.append((15, points))
        (positives if momentum_medium > 0 else risks).append(
            f"Impulso de tres meses {momentum_medium:+.1f}%"
        )

    relative_score = relative.score if relative is not None else None
    if relative_score is not None:
        parts.append((18, int(relative_score)))
        (positives if relative_score >= 55 else risks).append(
            f"Fortaleza frente al mercado y sector: {relative_score}/100"
        )

    distance_high = _number(row.get("distance_high_pct"))
    if distance_high is not None:
        near_high_points = 100 if distance_high >= -8 else 70 if distance_high >= -15 else 30
        parts.append((12, near_high_points))
        (positives if near_high_points >= 70 else risks).append(
            f"Distancia respecto al máximo observado: {distance_high:.1f}%"
        )

    breakout = bool(row.get("breakout", False))
    parts.append((6, 100 if breakout else 45))
    if breakout:
        positives.append("Acaba de superar un máximo reciente")
    volume_ratio = _number(row.get("volume_ratio"))
    if volume_ratio is not None:
        volume_points = 100 if volume_ratio >= 1.2 else 70 if volume_ratio >= 0.8 else 30
        parts.append((5, volume_points))

    distance_short = _number(row.get("distance_sma_short_pct"))
    extended = distance_short is not None and distance_short > 15.0
    if extended:
        risks.append(f"Está un {distance_short:.1f}% por encima de su media corta; conviene no perseguirla")
    return int(_weighted_score(parts) or 0), positives, risks, extended


def _market_context_score(
    frame: pd.DataFrame,
    broad_market: pd.DataFrame | None,
    risk: RiskResult | None,
    config: GrowthMomentumConfig,
) -> tuple[int, int, list[str], list[str]]:
    parts: list[tuple[float, int]] = []
    positives: list[str] = []
    risks: list[str] = []
    coverage = 0

    if broad_market is not None and not broad_market.empty and "close" in broad_market:
        market_close = pd.to_numeric(broad_market["close"], errors="coerce").dropna()
        if len(market_close) >= 200:
            latest = float(market_close.iloc[-1])
            sma_200 = float(market_close.tail(200).mean())
            sma_50_now = float(market_close.tail(50).mean())
            sma_50_prior = float(market_close.iloc[-55:-5].mean()) if len(market_close) >= 55 else sma_50_now
            market_up = latest > sma_200
            market_improving = sma_50_now > sma_50_prior
            parts.extend(((20, 100 if market_up else 10), (10, 100 if market_improving else 25)))
            coverage += 30
            (positives if market_up else risks).append(
                "El mercado de referencia está sobre su media larga"
                if market_up
                else "El mercado de referencia está por debajo de su media larga"
            )

    latest_frame = frame.dropna(subset=["close"])
    turnover = None
    if not latest_frame.empty and "volume" in latest_frame:
        turnover_series = (
            pd.to_numeric(latest_frame["close"], errors="coerce")
            * pd.to_numeric(latest_frame["volume"], errors="coerce")
        )
        turnover = _number(turnover_series.tail(20).mean())
    if turnover is not None:
        liquidity_score = 100 if turnover >= config.min_turnover_eur else 55 if turnover >= config.min_turnover_eur / 2 else 15
        parts.append((25, liquidity_score))
        coverage += 25
        (positives if liquidity_score >= 55 else risks).append(
            f"Negociación diaria aproximada: {turnover / 1_000_000:.1f} millones"
        )

    if risk is not None and risk.score is not None:
        parts.append((25, int(risk.score)))
        coverage += 25
        (positives if risk.score >= 55 else risks).append(
            f"Control estadístico de volatilidad y caída: {risk.score}/100"
        )
    if risk is not None and risk.max_drawdown_1y_pct is not None:
        drawdown = float(risk.max_drawdown_1y_pct)
        points = 100 if drawdown >= -15 else 70 if drawdown >= -30 else 30 if drawdown >= -50 else 10
        parts.append((20, points))
        coverage += 20
        if drawdown < -30:
            risks.append(f"Ha sufrido una caída máxima anual elevada ({drawdown:.1f}%)")

    return int(_weighted_score(parts) or 0), coverage, positives, risks


def evaluate_growth_momentum(
    *,
    ticker: str,
    frame: pd.DataFrame,
    info: dict[str, Any],
    relative: RelativeStrengthResult | None,
    risk: RiskResult | None,
    broad_market: pd.DataFrame | None,
    config: GrowthMomentumConfig,
    sector_override: str | None = None,
) -> GrowthMomentumResult:
    """Evalúa una empresa sin reutilizar la etiqueta del motor equilibrado."""

    config.validate()
    profile = (
        SECTOR_PROFILES[sector_override]
        if sector_override in SECTOR_PROFILES
        else classify_sector_profile(info, ticker)
    )
    market_cap = _number(info.get("marketCap"))
    is_small_cap = market_cap is not None and market_cap < 2_000_000_000
    growth_score, growth_coverage, growth_good, growth_risks = _growth_quality(info, profile)
    momentum_score, momentum_good, momentum_risks, extended = _momentum_score(frame, relative)
    context_score, context_coverage, context_good, context_risks = _market_context_score(
        frame,
        broad_market,
        risk,
        config,
    )

    components: list[tuple[float, int]] = [(40, momentum_score), (20, context_score)]
    if growth_score is not None:
        components.append((40, growth_score))
    score = int(_weighted_score(components) or 0)
    confidence = min(100, round(40 + context_coverage * 0.20 + growth_coverage * 0.40))

    risk_pct = config.normal_risk_pct * profile.risk_multiplier
    max_position_pct = profile.max_position_pct
    atr_multiplier = profile.atr_multiplier
    risks = [*growth_risks, *momentum_risks, *context_risks]
    manual_checks = list(profile.manual_checks)
    if is_small_cap:
        risk_pct *= 0.65
        max_position_pct = min(max_position_pct, 3.0)
        atr_multiplier = max(atr_multiplier, 2.8)
        risks.append("Es una small cap: se reduce el tamaño por liquidez, dilución y gaps")
        manual_checks.append("Revisar accionistas internos, ampliaciones y volumen real negociado.")
    if profile.key == "biotech":
        risks.append("La puntuación no incorpora la probabilidad clínica ni la autonomía de caja")

    valid = frame.dropna(subset=["close", "sma_medium", "sma_long"])
    latest = valid.iloc[-1]
    price = float(latest["close"])
    atr_pct = None
    atr = _number(latest.get("atr_14"))
    if atr is not None and price > 0:
        atr_pct = atr / price * 100.0
    stop_distance = float(
        np.clip(
            (atr_pct or config.min_stop_pct / atr_multiplier) * atr_multiplier,
            config.min_stop_pct,
            config.max_stop_pct,
        )
    )

    market_broken = context_score < 35
    if confidence < 45:
        label = "Datos insuficientes"
    elif extended and score >= config.candidate_score:
        label = "Esperar mejor precio"
    elif (
        score >= config.strong_score
        and momentum_score >= 75
        and context_score >= 50
        and not market_broken
    ):
        label = "Entrada fuerte"
    elif (
        score >= config.candidate_score
        and momentum_score >= 65
        and context_score >= 40
        and not market_broken
    ):
        label = "Entrada candidata"
    elif score >= config.watch_score:
        label = "Vigilancia activa"
    else:
        label = "No preparada"

    return GrowthMomentumResult(
        ticker=ticker,
        as_of=pd.Timestamp(valid.index[-1]),
        score=score,
        confidence_pct=confidence,
        label=label,
        growth_score=growth_score,
        momentum_score=momentum_score,
        context_score=context_score,
        sector_key=profile.key,
        sector_label=profile.label,
        is_small_cap=is_small_cap,
        price=price,
        suggested_risk_pct=round(risk_pct, 3),
        atr_stop_pct=round(stop_distance, 2),
        max_position_pct=max_position_pct,
        positive_factors=tuple([*growth_good, *momentum_good, *context_good]),
        risk_factors=tuple(dict.fromkeys(risks)),
        manual_checks=tuple(dict.fromkeys(manual_checks)),
    )


def calculate_growth_position_plan(
    *,
    result: GrowthMomentumResult,
    config: GrowthMomentumConfig,
    liquid_capital: float,
    monthly_investable: float,
    current_strategy_value: float = 0.0,
    current_sector_value: float = 0.0,
    current_open_risk: float = 0.0,
    entry_price: float | None = None,
    manual_stop_pct: float | None = None,
) -> GrowthPositionPlan:
    """Dimensiona una entrada con riesgo, presupuesto mensual y techo de cartera."""

    config.validate()
    if (
        liquid_capital <= 0
        or monthly_investable < 0
        or current_strategy_value < 0
        or current_sector_value < 0
        or current_open_risk < 0
    ):
        raise ValueError("Capital, aportación y valor actual deben ser cantidades válidas.")
    price = float(entry_price if entry_price is not None else result.price)
    if price <= 0:
        raise ValueError("El precio de entrada debe ser positivo.")
    stop_pct = float(manual_stop_pct if manual_stop_pct is not None else result.atr_stop_pct)
    if not 0 < stop_pct < 100:
        raise ValueError("La distancia al stop debe estar entre 0 y 100%.")

    monthly_budget = monthly_investable * config.monthly_allocation_pct / 100.0
    strategy_cap = liquid_capital * config.strategy_cap_pct / 100.0
    remaining_capacity = max(strategy_cap - current_strategy_value, 0.0)
    sector_cap = strategy_cap * config.max_sector_pct / 100.0
    remaining_sector_capacity = max(sector_cap - current_sector_value, 0.0)
    open_risk_cap = liquid_capital * config.max_open_risk_pct / 100.0
    remaining_open_risk = max(open_risk_cap - current_open_risk, 0.0)
    risk_budget = min(
        liquid_capital * result.suggested_risk_pct / 100.0,
        remaining_open_risk,
    )
    value_by_risk = risk_budget / (stop_pct / 100.0)
    value_by_position_cap = liquid_capital * result.max_position_pct / 100.0
    suggested_value = max(
        0.0,
        min(
            monthly_budget,
            remaining_capacity,
            remaining_sector_capacity,
            value_by_risk,
            value_by_position_cap,
        ),
    )
    quantity = suggested_value / price
    stop_price = price * (1 - stop_pct / 100.0)
    target_2r = price * (1 + 2 * stop_pct / 100.0)
    loss_at_stop = suggested_value * stop_pct / 100.0
    commission = config.commission_per_order_eur * 2.0
    commission_drag = commission / suggested_value * 100.0 if suggested_value > 0 else 0.0
    return GrowthPositionPlan(
        monthly_strategy_budget=monthly_budget,
        remaining_strategy_capacity=remaining_capacity,
        remaining_sector_capacity=remaining_sector_capacity,
        remaining_open_risk=remaining_open_risk,
        risk_budget=risk_budget,
        suggested_position_value=suggested_value,
        quantity=quantity,
        stop_distance_pct=stop_pct,
        stop_price=stop_price,
        reference_target_2r=target_2r,
        loss_at_stop=loss_at_stop,
        round_trip_commission=commission,
        commission_drag_pct=commission_drag,
    )
