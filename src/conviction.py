"""Checklist explicable de convicción empresarial a tres-cinco años.

La calidad del negocio se mantiene separada de valoración, momento técnico y
encaje personal en cartera. Las métricas ausentes reducen la cobertura en vez
de convertirse en un cero, y los criterios cualitativos requieren una respuesta
explícita del usuario.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


STATUS_STRONG = "Cumple"
STATUS_PARTIAL = "Parcial"
STATUS_WEAK = "No cumple"
STATUS_MISSING = "Sin datos"
STATUS_NOT_APPLICABLE = "No comparable"
STATUS_MANUAL = "Por validar"

MANUAL_OPTIONS = ("Sin revisar", "No", "Parcial / con dudas", "Sí")
MANUAL_POINTS = {
    "No": 0.0,
    "Parcial / con dudas": 1.0,
    "Sí": 2.0,
}


@dataclass(frozen=True)
class ConvictionCheck:
    key: str
    block: str
    question: str
    status: str
    value: str
    rule: str
    evidence: str
    automatic: bool
    weight: float

    @property
    def counts_for_score(self) -> bool:
        return self.weight > 0


@dataclass(frozen=True)
class ConvictionResult:
    ticker: str
    sector: str
    sector_profile: str
    automatic_score: int | None
    automatic_coverage_pct: int
    label: str
    checks: tuple[ConvictionCheck, ...]


@dataclass(frozen=True)
class ConvictionSummary:
    automatic_score: int | None
    automatic_coverage_pct: int
    manual_score: int | None
    manual_coverage_pct: int
    manual_answered: int
    manual_total: int
    combined_score: int | None
    label: str


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _sector_profile(sector: str) -> str:
    normalized = sector.casefold()
    if any(word in normalized for word in ("financial", "bank", "insurance")):
        return "financial"
    if any(word in normalized for word in ("utility", "real estate", "reit")):
        return "capital_intensive"
    if any(word in normalized for word in ("energy", "basic materials")):
        return "cyclical"
    if any(
        word in normalized
        for word in ("technology", "communication", "healthcare")
    ):
        return "growth"
    return "general"


def _format_pct(value: float | None) -> str:
    return "N/D" if value is None else f"{value:.1%}"


def _format_multiple(value: float | None) -> str:
    return "N/D" if value is None else f"{value:.1f}x"


def _format_money(value: float | None) -> str:
    if value is None:
        return "N/D"
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f} mil M"
    if absolute >= 1_000_000:
        return f"{value / 1_000_000:.1f} M"
    return f"{value:,.0f}"


def _status(
    value: float | None,
    *,
    strong: bool,
    partial: bool,
) -> str:
    if value is None:
        return STATUS_MISSING
    if strong:
        return STATUS_STRONG
    if partial:
        return STATUS_PARTIAL
    return STATUS_WEAK


def _manual_check(
    key: str,
    block: str,
    question: str,
    evidence: str,
    *,
    weight: float = 1.0,
) -> ConvictionCheck:
    return ConvictionCheck(
        key=key,
        block=block,
        question=question,
        status=STATUS_MANUAL,
        value="Pendiente",
        rule="Responder con evidencia, no por intuición",
        evidence=evidence,
        automatic=False,
        weight=weight,
    )


def evaluate_conviction(
    info: Mapping[str, Any],
    ticker: str = "",
    *,
    entry_score: int | None = None,
) -> ConvictionResult:
    """Evalúa las 22 preguntas de la hoja sin mezclar calidad y entrada."""

    symbol = (ticker or str(info.get("symbol") or "Activo")).strip().upper()
    sector = str(info.get("sector") or "Sector no identificado").strip()
    profile = _sector_profile(sector)
    checks: list[ConvictionCheck] = []

    def add(
        key: str,
        block: str,
        question: str,
        status: str,
        value: str,
        rule: str,
        evidence: str,
        *,
        weight: float = 1.0,
    ) -> None:
        checks.append(
            ConvictionCheck(
                key=key,
                block=block,
                question=question,
                status=status,
                value=value,
                rule=rule,
                evidence=evidence,
                automatic=True,
                weight=weight,
            )
        )

    market_cap = _number(info.get("marketCap"))
    total_cash = _number(info.get("totalCash"))
    cash_ratio = (
        total_cash / market_cap
        if total_cash is not None and market_cap not in (None, 0)
        else None
    )
    if profile == "financial":
        cash_status = STATUS_NOT_APPLICABLE
        cash_rule = "En entidades financieras la caja forma parte del negocio"
    else:
        cash_status = _status(
            cash_ratio,
            strong=cash_ratio is not None and cash_ratio >= 0.10,
            partial=cash_ratio is not None and cash_ratio >= 0.05,
        )
        cash_rule = "Caja ≥ 10% de la capitalización; 5–10% es parcial"
    add(
        "cash_buffer",
        "Balance y rentabilidad",
        "¿Dispone de un 10% o más en caja?",
        cash_status,
        _format_pct(cash_ratio),
        cash_rule,
        "Se utiliza caja/capitalización como referencia; no equivale a caja neta.",
        weight=1.0,
    )

    checks.append(
        _manual_check(
            "stable_quarters",
            "Balance y rentabilidad",
            "¿Lleva varios trimestres con resultados estables?",
            "Revisar 8–12 trimestres de ventas, margen y flujo de caja, incluyendo los malos.",
            weight=1.25,
        )
    )

    pe = _number(info.get("forwardPE")) or _number(info.get("trailingPE"))
    pe_limit = 35.0 if profile == "growth" else 25.0 if profile == "general" else 20.0
    pe_status = _status(
        pe,
        strong=pe is not None and 0 < pe <= pe_limit,
        partial=pe is not None and 0 < pe <= pe_limit * 1.5,
    )
    add(
        "pe_context",
        "Balance y rentabilidad",
        "¿Conoces cuál es su PER actual y su contexto?",
        pe_status,
        _format_multiple(pe),
        f"Comparar con sector, crecimiento e histórico; referencia {pe_limit:.0f}x",
        "Es valoración, no calidad empresarial; por eso no suma al score de convicción.",
        weight=0.0,
    )

    price_to_book = _number(info.get("priceToBook"))
    if profile == "growth":
        book_status = STATUS_NOT_APPLICABLE
        book_rule = "El valor contable suele ser poco representativo en negocios intangibles"
    else:
        book_limit = 2.0 if profile == "financial" else 4.0
        book_status = _status(
            price_to_book,
            strong=price_to_book is not None and 0 < price_to_book <= book_limit,
            partial=price_to_book is not None and 0 < price_to_book <= book_limit * 1.5,
        )
        book_rule = f"Precio/valor contable ≤ {book_limit:.1f}x para este perfil"
    add(
        "book_support",
        "Balance y rentabilidad",
        "¿El valor contable supera el 25% de la capitalización?",
        book_status,
        _format_multiple(price_to_book),
        book_rule,
        "Equivale aproximadamente a precio/valor contable ≤ 4x, pero depende del sector.",
        weight=0.0,
    )

    operating_margin = _number(info.get("operatingMargins"))
    add(
        "double_digit_margin",
        "Balance y rentabilidad",
        "¿Maneja márgenes operativos de dos dígitos?",
        _status(
            operating_margin,
            strong=operating_margin is not None and operating_margin >= 0.10,
            partial=operating_margin is not None and operating_margin >= 0.05,
        ) if profile != "financial" else STATUS_NOT_APPLICABLE,
        _format_pct(operating_margin),
        "Margen operativo ≥ 10%; 5–10% es parcial",
        "Debe compararse con competidores y con su propia estabilidad histórica.",
        weight=1.0,
    )

    gross_margin = _number(info.get("grossMargins"))
    gross_limit = 0.30 if profile in {"growth", "general"} else 0.20
    add(
        "high_gross_margin",
        "Balance y rentabilidad",
        "¿Maneja márgenes brutos superiores al 30%?",
        _status(
            gross_margin,
            strong=gross_margin is not None and gross_margin >= gross_limit,
            partial=gross_margin is not None and gross_margin >= gross_limit * 0.70,
        ) if profile != "financial" else STATUS_NOT_APPLICABLE,
        _format_pct(gross_margin),
        f"Umbral sectorial aplicado: {gross_limit:.0%}",
        "Se adapta el 30% para no penalizar automáticamente sectores intensivos o cíclicos.",
        weight=0.75,
    )

    checks.append(
        _manual_check(
            "working_capital_advantage",
            "Caja y capital",
            "¿Cobra al contado o por adelantado y paga a plazos?",
            "Comprobar ciclo de conversión de caja, ingresos diferidos y dependencia de proveedores.",
            weight=0.75,
        )
    )

    roic = _number(info.get("returnOnInvestedCapital"))
    roic_status = (
        STATUS_NOT_APPLICABLE
        if profile == "financial"
        else _status(
            roic,
            strong=roic is not None and roic >= 0.20,
            partial=roic is not None and roic >= 0.10,
        )
    )
    add(
        "roic",
        "Caja y capital",
        "¿El ROIC es superior al 20%?",
        roic_status,
        _format_pct(roic),
        "ROIC ≥ 20%; 10–20% es parcial",
        "Conviene validar una mediana de 3–5 años; una sola fotografía puede engañar.",
        weight=1.5,
    )

    operating_cash = _number(info.get("operatingCashflow"))
    capex = _number(info.get("capitalExpenditures"))
    capex_ratio = (
        abs(capex) / abs(operating_cash)
        if capex is not None and operating_cash not in (None, 0)
        else None
    )
    capex_strong = 0.80 if profile in {"capital_intensive", "cyclical"} else 0.40
    capex_partial = 1.20 if profile in {"capital_intensive", "cyclical"} else 0.75
    add(
        "capital_intensity",
        "Caja y capital",
        "¿El negocio evita inversiones constantes desproporcionadas?",
        _status(
            capex_ratio,
            strong=capex_ratio is not None and capex_ratio <= capex_strong,
            partial=capex_ratio is not None and capex_ratio <= capex_partial,
        ),
        _format_pct(capex_ratio),
        f"Capex/flujo operativo ≤ {capex_strong:.0%}; umbral adaptado al sector",
        "No se premia simplemente gastar poco: importa cuánto retorno produce la reinversión.",
        weight=0.75,
    )

    debt_to_equity = _number(info.get("debtToEquity"))
    if profile == "financial":
        debt_status = STATUS_NOT_APPLICABLE
        debt_rule = "Bancos y aseguradoras necesitan ratios regulatorios específicos"
    else:
        debt_strong = 100.0 if profile in {"capital_intensive", "cyclical"} else 50.0
        debt_partial = 200.0 if profile in {"capital_intensive", "cyclical"} else 100.0
        debt_status = _status(
            debt_to_equity,
            strong=debt_to_equity is not None and debt_to_equity <= debt_strong,
            partial=debt_to_equity is not None and debt_to_equity <= debt_partial,
        )
        debt_rule = f"Deuda/patrimonio ≤ {debt_strong / 100:.1f}x; adaptación sectorial"
    add(
        "long_term_debt",
        "Caja y capital",
        "¿Las deudas a largo plazo son bajas o controlables?",
        debt_status,
        "N/D" if debt_to_equity is None else f"{debt_to_equity / 100:.2f}x",
        debt_rule,
        "Debe completarse con cobertura de intereses y calendario de vencimientos.",
        weight=1.25,
    )

    revenue_growth = _number(info.get("revenueGrowth"))
    earnings_growth = _number(info.get("earningsGrowth"))
    growth_value = (
        min(revenue_growth, earnings_growth)
        if revenue_growth is not None and earnings_growth is not None
        else revenue_growth if revenue_growth is not None else earnings_growth
    )
    both_growing = (
        revenue_growth is not None
        and earnings_growth is not None
        and revenue_growth >= 0.08
        and earnings_growth >= 0.08
    )
    not_contracting = (
        growth_value is not None
        and growth_value > 0
        and (revenue_growth is None or revenue_growth >= 0)
        and (earnings_growth is None or earnings_growth >= 0)
    )
    add(
        "sustained_growth",
        "Crecimiento y dirección",
        "¿Se aprecia crecimiento sostenido en sus cuentas?",
        _status(growth_value, strong=both_growing, partial=not_contracting),
        (
            f"Ventas {_format_pct(revenue_growth)} · beneficio {_format_pct(earnings_growth)}"
        ),
        "Ventas y beneficio ≥ 8%; el último periodo es sólo una aproximación",
        "La respuesta definitiva exige revisar varios ejercicios y trimestres.",
        weight=1.5,
    )

    checks.append(
        _manual_check(
            "active_expansion",
            "Crecimiento y dirección",
            "¿Está trabajando activamente para ampliar el negocio?",
            "Buscar nuevas líneas rentables, capacidad, clientes o mercados con objetivos medibles.",
        )
    )
    checks.append(
        _manual_check(
            "management_value",
            "Crecimiento y dirección",
            "¿La directiva ha demostrado crear valor y asignar bien el capital?",
            "Revisar adquisiciones, ventas, deuda, dilución y retorno de las reinversiones.",
            weight=1.25,
        )
    )

    shares_change = _number(info.get("sharesChangeYoY"))
    if shares_change is None:
        checks.append(
            _manual_check(
                "buyback",
                "Crecimiento y dirección",
                "¿Tiene recompras que reduzcan realmente las acciones en circulación?",
                "No basta con anunciar el programa: comprobar acciones diluidas y precio pagado.",
                weight=0.5,
            )
        )
    else:
        add(
            "buyback",
            "Crecimiento y dirección",
            "¿Tiene recompras que reduzcan realmente las acciones en circulación?",
            _status(
                shares_change,
                strong=shares_change <= -0.01,
                partial=shares_change <= 0.0,
            ),
            _format_pct(shares_change),
            "Reducción interanual ≥ 1%; no sólo autorización de recompra",
            "Una recompra cara o financiada con deuda puede destruir valor.",
            weight=0.5,
        )

    checks.append(
        _manual_check(
            "outlook_3_5_years",
            "Crecimiento y dirección",
            "¿Cómo ves el negocio dentro de 3–5 años?",
            "Escribir escenario bajista, base y alcista, junto con qué invalidaría la tesis.",
            weight=1.5,
        )
    )
    checks.append(
        _manual_check(
            "understand_business",
            "Ventaja competitiva",
            "¿Entiendes claramente cómo gana dinero?",
            "Explicar en dos frases cliente, producto, precio, coste y razón de recompra.",
            weight=1.25,
        )
    )
    checks.append(
        _manual_check(
            "moat",
            "Ventaja competitiva",
            "¿Tiene una posición privilegiada dentro de su sector?",
            "Validar costes de cambio, red, regulación, escala, marca o propiedad intelectual.",
            weight=1.25,
        )
    )
    checks.append(
        _manual_check(
            "differentiation",
            "Ventaja competitiva",
            "¿Su producto ofrece mejoras diferenciales sobre la competencia?",
            "Buscar evidencia en retención, precios, cuota y márgenes, no sólo en marketing.",
        )
    )
    checks.append(
        _manual_check(
            "international_expansion",
            "Ventaja competitiva",
            "¿Tiene presencia o expansión internacional razonable?",
            "Es contexto, no obligación: crecer fuera sólo suma si mantiene rentabilidad.",
            weight=0.0,
        )
    )

    entry_value = float(entry_score) if entry_score is not None else None
    add(
        "entry_now",
        "Entrada y encaje",
        "¿Existe una razón de peso para entrar ahora?",
        _status(
            entry_value,
            strong=entry_value is not None and entry_value >= 70,
            partial=entry_value is not None and entry_value >= 55,
        ),
        "N/D" if entry_score is None else f"{entry_score}/100",
        "Momento ≥ 70; 55–69 permanece en vigilancia",
        "Se muestra como contexto técnico y no modifica la convicción empresarial.",
        weight=0.0,
    )

    add(
        "institutional_capacity",
        "Entrada y encaje",
        "¿Tiene tamaño suficiente para atraer fondos relevantes?",
        _status(
            market_cap,
            strong=market_cap is not None and market_cap >= 2_000_000_000,
            partial=market_cap is not None and market_cap >= 500_000_000,
        ),
        _format_money(market_cap),
        "≥ 2.000 M; 500–2.000 M es parcial y requiere revisar liquidez",
        "Es capacidad de negociación, no garantía de que entren instituciones.",
        weight=0.0,
    )

    checks.append(
        _manual_check(
            "sleep_well",
            "Entrada y encaje",
            "¿El tamaño de esta posición te permitiría dormir tranquilo?",
            "Depende de volatilidad, concentración, tesis y pérdida asumible; no puntúa la empresa.",
            weight=0.0,
        )
    )

    applicable = [
        check
        for check in checks
        if check.automatic
        and check.counts_for_score
        and check.status != STATUS_NOT_APPLICABLE
    ]
    available = [check for check in applicable if check.status != STATUS_MISSING]
    applicable_weight = sum(check.weight for check in applicable)
    available_weight = sum(check.weight for check in available)
    coverage = (
        round(available_weight / applicable_weight * 100)
        if applicable_weight
        else 0
    )
    status_points = {
        STATUS_STRONG: 2.0,
        STATUS_PARTIAL: 1.0,
        STATUS_WEAK: 0.0,
    }
    earned = sum(status_points.get(check.status, 0.0) * check.weight for check in available)
    automatic_score = (
        round(earned / (available_weight * 2.0) * 100)
        if available_weight and coverage >= 40
        else None
    )
    if automatic_score is None:
        label = "Datos empresariales insuficientes"
    elif automatic_score >= 80:
        label = "Calidad empresarial alta"
    elif automatic_score >= 65:
        label = "Empresa interesante para profundizar"
    elif automatic_score >= 50:
        label = "Calidad mixta"
    else:
        label = "Convicción empresarial débil"
    return ConvictionResult(
        ticker=symbol,
        sector=sector,
        sector_profile=profile,
        automatic_score=automatic_score,
        automatic_coverage_pct=coverage,
        label=label,
        checks=tuple(checks),
    )


def summarize_conviction(
    result: ConvictionResult,
    answers: Mapping[str, str] | None = None,
) -> ConvictionSummary:
    """Combina datos y respuestas sólo cuando la cobertura manual es suficiente."""

    responses = answers or {}
    manual = [
        check
        for check in result.checks
        if not check.automatic and check.counts_for_score
    ]
    answered = [
        check for check in manual if responses.get(check.key) in MANUAL_POINTS
    ]
    manual_weight = sum(check.weight for check in manual)
    answered_weight = sum(check.weight for check in answered)
    manual_coverage = (
        round(answered_weight / manual_weight * 100) if manual_weight else 0
    )
    earned = sum(
        MANUAL_POINTS[str(responses[check.key])] * check.weight for check in answered
    )
    manual_score = (
        round(earned / (answered_weight * 2.0) * 100)
        if answered_weight and manual_coverage >= 50
        else None
    )
    combined = (
        round(result.automatic_score * 0.60 + manual_score * 0.40)
        if result.automatic_score is not None and manual_score is not None
        else None
    )
    if combined is None:
        label = "Completar tesis cualitativa"
    elif combined >= 80:
        label = "Convicción alta"
    elif combined >= 65:
        label = "Convicción suficiente para estudiar entrada"
    elif combined >= 50:
        label = "Convicción mixta"
    else:
        label = "Tesis débil o incompleta"
    return ConvictionSummary(
        automatic_score=result.automatic_score,
        automatic_coverage_pct=result.automatic_coverage_pct,
        manual_score=manual_score,
        manual_coverage_pct=manual_coverage,
        manual_answered=len(answered),
        manual_total=len(manual),
        combined_score=combined,
        label=label,
    )
