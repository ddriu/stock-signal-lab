"""Descubrimiento y control de oportunidades especulativas.

El screener sólo crea un universo de estudio. Una empresa no se presenta como
apta hasta superar después la misma evaluación conjunta de ``Entradas`` y unos
controles adicionales de liquidez, tamaño y persecución del precio.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from src.entry_opportunity import EntryOpportunityResult, STATUS_BUYABLE
from src.opportunity import RiskResult


ALLOWED_EXCHANGES = frozenset({"NMS", "NGM", "NCM", "NYQ", "NASDAQ", "NYSE"})
MIN_MARKET_CAP = 100_000_000
MAX_MARKET_CAP = 2_500_000_000
MIN_PRICE = 1.0
MIN_AVERAGE_VOLUME = 250_000
MIN_DAILY_TURNOVER = 2_000_000
MIN_RISK_REWARD = 2.0
MIN_CONFIDENCE = 60
MAX_DAILY_JUMP_PCT = 15.0
RATE_LIMIT_MARKERS = (
    "too many requests",
    "rate limit",
    "rate limited",
    "429",
)


class SpeculativeDiscoveryRateLimited(RuntimeError):
    """El proveedor externo ha rechazado temporalmente nuevas consultas."""


def is_speculative_rate_limit_error(error: BaseException) -> bool:
    """Reconoce las variantes habituales de un bloqueo temporal del proveedor."""

    if isinstance(error, SpeculativeDiscoveryRateLimited):
        return True
    description = f"{type(error).__name__}: {error}".lower()
    return any(marker in description for marker in RATE_LIMIT_MARKERS)


def speculative_discovery_error_message(error: BaseException) -> str:
    """Traduce errores técnicos sin exponerlos directamente en la interfaz."""

    if is_speculative_rate_limit_error(error):
        return (
            "El proveedor gratuito ha limitado temporalmente las consultas del "
            "screener. Tus favoritas y el último resultado válido se conservan."
        )
    return (
        "No se pudo consultar ahora el universo externo. Tus favoritas y el último "
        "resultado válido se conservan; puedes seguir usando el resto del análisis."
    )


@dataclass(frozen=True)
class SpeculativeCandidate:
    ticker: str
    company_name: str
    exchange: str
    price: float
    market_cap: float
    average_volume_3m: float
    daily_turnover: float
    daily_change_pct: float | None = None


@dataclass(frozen=True)
class SpeculativeAssessment:
    ticker: str
    eligible: bool
    label: str
    score: int
    market_cap: float | None
    daily_turnover: float | None
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]


def _number(value: Any) -> float | None:
    try:
        resolved = float(value)
    except (TypeError, ValueError):
        return None
    return resolved if resolved == resolved else None


def discover_speculative_candidates(
    *,
    limit: int = 12,
    screener: Callable[..., dict[str, Any]] | None = None,
) -> list[SpeculativeCandidate]:
    """Busca small caps líquidas de EE. UU. sin aceptar OTC ni penny stocks < 1$."""

    if limit <= 0:
        return []
    try:
        if screener is None:
            import yfinance as yf
            from yfinance import EquityQuery

            query = EquityQuery(
                "and",
                [
                    EquityQuery("eq", ["region", "us"]),
                    EquityQuery(
                        "is-in", ["exchange", "NMS", "NGM", "NCM", "NYQ"]
                    ),
                    EquityQuery(
                        "btwn",
                        ["intradaymarketcap", MIN_MARKET_CAP, MAX_MARKET_CAP],
                    ),
                    EquityQuery("gte", ["intradayprice", MIN_PRICE]),
                    EquityQuery("gte", ["avgdailyvol3m", MIN_AVERAGE_VOLUME]),
                ],
            )
            response = yf.screen(
                query,
                size=min(max(limit * 5, 50), 250),
                sortField="avgdailyvol3m",
                sortAsc=False,
            )
        else:
            response = screener()
    except Exception as exc:
        if is_speculative_rate_limit_error(exc):
            raise SpeculativeDiscoveryRateLimited(
                "El proveedor ha limitado temporalmente el screener."
            ) from exc
        raise

    candidates: list[SpeculativeCandidate] = []
    seen: set[str] = set()
    for quote in response.get("quotes", []) if isinstance(response, dict) else []:
        ticker = str(quote.get("symbol") or "").strip().upper()
        exchange = str(quote.get("exchange") or "").strip().upper()
        quote_type = str(quote.get("quoteType") or "").strip().upper()
        price = _number(quote.get("regularMarketPrice"))
        market_cap = _number(quote.get("marketCap"))
        average_volume = _number(quote.get("averageDailyVolume3Month"))
        if (
            not ticker
            or ticker in seen
            or quote_type != "EQUITY"
            or exchange not in ALLOWED_EXCHANGES
            or price is None
            or price < MIN_PRICE
            or market_cap is None
            or not MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP
            or average_volume is None
            or average_volume < MIN_AVERAGE_VOLUME
        ):
            continue
        turnover = price * average_volume
        if turnover < MIN_DAILY_TURNOVER:
            continue
        seen.add(ticker)
        candidates.append(
            SpeculativeCandidate(
                ticker=ticker,
                company_name=str(
                    quote.get("longName") or quote.get("shortName") or ticker
                ).strip(),
                exchange=exchange,
                price=price,
                market_cap=market_cap,
                average_volume_3m=average_volume,
                daily_turnover=turnover,
                daily_change_pct=_number(quote.get("regularMarketChangePercent")),
            )
        )
        if len(candidates) >= limit:
            break
    return candidates


def assess_speculative_candidate(
    opportunity: EntryOpportunityResult,
    risk: RiskResult,
    info: dict[str, Any],
    discovery: SpeculativeCandidate | None = None,
) -> SpeculativeAssessment:
    """Exige una entrada comprable y controles más estrictos que el radar general."""

    market_cap = _number(info.get("marketCap"))
    if market_cap is None and discovery is not None:
        market_cap = discovery.market_cap
    exchange = str(info.get("exchange") or "").strip().upper()
    if not exchange and discovery is not None:
        exchange = discovery.exchange
    turnover = risk.average_turnover_20d
    if turnover is None and discovery is not None:
        turnover = discovery.daily_turnover

    reasons: list[str] = []
    warnings: list[str] = []
    blockers: list[str] = []
    if opportunity.status_code != STATUS_BUYABLE:
        blockers.append("no supera todos los filtros de entrada")
    if market_cap is None or not MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP:
        blockers.append("capitalización fuera del rango especulativo controlado")
    else:
        reasons.append("small cap dentro del rango definido")
    if exchange not in ALLOWED_EXCHANGES:
        blockers.append("mercado no admitido u OTC")
    else:
        reasons.append("cotiza en un mercado admitido")
    if opportunity.price < MIN_PRICE:
        blockers.append("precio inferior a 1 USD")
    if turnover is None or turnover < MIN_DAILY_TURNOVER:
        blockers.append("liquidez monetaria insuficiente")
    else:
        reasons.append("liquidez mínima superada")
    if opportunity.confidence_pct < MIN_CONFIDENCE:
        blockers.append("cobertura de datos insuficiente")
    if (
        opportunity.zones.risk_reward is None
        or opportunity.zones.risk_reward < MIN_RISK_REWARD
    ):
        blockers.append("beneficio/riesgo inferior a 2:1")
    else:
        reasons.append("beneficio/riesgo de al menos 2:1")
    daily_jump = opportunity.timing.return_1d_pct
    if daily_jump is not None and daily_jump > MAX_DAILY_JUMP_PCT:
        blockers.append("subida diaria demasiado explosiva para perseguirla")

    if info.get("freeCashflow") is None:
        warnings.append("caja y consumo de efectivo no verificados completamente")
    warnings.append("dilución y promociones deben comprobarse antes de operar")

    liquidity_score = min(100.0, max(0.0, (turnover or 0) / 100_000_000 * 100))
    reward_risk_score = min(
        100.0,
        max(0.0, float(opportunity.zones.risk_reward or 0) / 4.0 * 100),
    )
    score = round(
        opportunity.opportunity_score * 0.50
        + opportunity.timing.score * 0.20
        + liquidity_score * 0.15
        + reward_risk_score * 0.15
    )
    eligible = not blockers
    return SpeculativeAssessment(
        ticker=opportunity.ticker,
        eligible=eligible,
        label="Candidata especulativa" if eligible else "Descartada",
        score=max(0, min(100, score)),
        market_cap=market_cap,
        daily_turnover=turnover,
        reasons=tuple(reasons if eligible else blockers),
        warnings=tuple(warnings),
    )


def rank_speculative_assessments(
    assessments: Iterable[SpeculativeAssessment],
) -> list[SpeculativeAssessment]:
    return sorted(
        assessments,
        key=lambda item: (item.eligible, item.score),
        reverse=True,
    )
