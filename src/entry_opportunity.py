"""Capa de decisión de entrada sobre las señales existentes.

El score técnico conserva su significado original. Este módulo añade una
lectura independiente del precio actual (timing), zonas técnicas orientativas,
riesgo de eventos y una nota conjunta cuya cobertura siempre queda visible.
Ninguna de estas notas representa una probabilidad calibrada de beneficio.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import html
from typing import Any, Iterable

import numpy as np
import pandas as pd

from src.signal_engine import LABEL_BUY, LABEL_STRONG, SignalResult


STATUS_BUYABLE = "COMPRABLE"
STATUS_WAIT_PRICE = "ESPERAR_PRECIO"
STATUS_EXTENDED = "EXTENDIDA"
STATUS_EVENT = "EVENTO_NO_ENTRAR"

CHASE_REASONABLE = "Entrada razonable"
CHASE_SOMEWHAT_EXTENDED = "Algo extendida"
CHASE_WAIT_PULLBACK = "Esperar retroceso"
CHASE_DO_NOT = "No perseguir"


@dataclass(frozen=True)
class OpportunityScoringConfig:
    """Pesos y umbrales centralizados para evitar números dispersos."""

    technical_weight: float = 25.0
    timing_weight: float = 25.0
    fundamental_weight: float = 20.0
    valuation_weight: float = 10.0
    relative_weight: float = 10.0
    risk_weight: float = 5.0
    reward_risk_weight: float = 5.0
    minimum_buyable_score: int = 72
    minimum_buyable_timing: int = 62
    near_event_days: int = 3
    near_event_penalty: int = 15

    def validate(self) -> None:
        weights = (
            self.technical_weight,
            self.timing_weight,
            self.fundamental_weight,
            self.valuation_weight,
            self.relative_weight,
            self.risk_weight,
            self.reward_risk_weight,
        )
        if any(weight < 0 for weight in weights) or sum(weights) <= 0:
            raise ValueError("Los pesos de oportunidad deben ser positivos.")
        if not 0 <= self.minimum_buyable_score <= 100:
            raise ValueError("El umbral comprable debe estar entre 0 y 100.")
        if not 0 <= self.minimum_buyable_timing <= 100:
            raise ValueError("El umbral de timing debe estar entre 0 y 100.")
        if self.near_event_days < 0 or self.near_event_penalty < 0:
            raise ValueError("Los parámetros de eventos no pueden ser negativos.")


DEFAULT_OPPORTUNITY_CONFIG = OpportunityScoringConfig()


@dataclass(frozen=True)
class PriceZone:
    lower: float
    upper: float

    @property
    def label(self) -> str:
        return f"{self.lower:.2f}–{self.upper:.2f}"


@dataclass(frozen=True)
class EntryZones:
    current_price: float
    aggressive_entry: PriceZone
    preferred_entry: PriceZone
    excellent_entry: PriceZone
    invalidation: float
    breakout: float | None
    target: float | None
    risk_to_stop_pct: float | None
    risk_reward: float | None
    basis: str


@dataclass(frozen=True)
class EventRisk:
    earnings_date: date | None
    days_until: int | None
    label: str
    blocked: bool
    penalty: int
    verified: bool


@dataclass(frozen=True)
class TimingResult:
    ticker: str
    as_of: pd.Timestamp
    score_before_event: int
    score: int
    chase_label: str
    signal_price: float
    signal_date: pd.Timestamp
    gap_from_signal_pct: float
    gap_from_previous_close_pct: float
    distance_sma20_pct: float | None
    distance_sma50_pct: float | None
    distance_breakout_pct: float | None
    distance_high_20d_pct: float | None
    distance_high_52w_pct: float | None
    return_1d_pct: float | None
    return_5d_pct: float | None
    return_20d_pct: float | None
    return_60d_pct: float | None
    rsi: float | None
    atr_pct: float | None
    volume_ratio: float | None
    sma20: float | None
    sma50: float | None
    sma200: float | None
    macd: float | None
    macd_signal: float | None
    breakout_price: float | None
    positive_factors: tuple[str, ...]
    risk_factors: tuple[str, ...]


@dataclass(frozen=True)
class EntryOpportunityResult:
    ticker: str
    company_name: str
    price: float
    technical_score: int
    technical_label: str
    timing: TimingResult
    fundamental_score: int | None
    opportunity_score: int
    confidence_pct: int
    status_code: str
    status_label: str
    event: EventRisk
    zones: EntryZones
    sector: str
    market: str
    position_action: str
    explanation: str
    positive_factors: tuple[str, ...]
    risk_factors: tuple[str, ...]


def technical_score_label(score: int) -> str:
    if score >= 90:
        return "Momentum excepcional"
    if score >= 80:
        return "Momento técnico fuerte"
    if score >= 65:
        return "Entrada interesante"
    if score >= 50:
        return "Neutral / esperar"
    return "Entrada débil"


def non_linking_ticker_text(ticker: str) -> str:
    """Conserva el ticker visible sin que el punto parezca un dominio web."""

    normalized = str(ticker or "").strip().upper()
    return normalized.replace(".", ".\u2060")


def ticker_display_html(ticker: str) -> str:
    return f"<span>{html.escape(non_linking_ticker_text(ticker))}</span>"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _score(value: int | float) -> int:
    return int(round(float(np.clip(value, 0, 100))))


def _period_return(close: pd.Series, sessions: int) -> float | None:
    values = pd.to_numeric(close, errors="coerce").dropna()
    if len(values) <= sessions:
        return None
    initial = float(values.iloc[-sessions - 1])
    if initial <= 0:
        return None
    return (float(values.iloc[-1]) / initial - 1.0) * 100.0


def _pct_distance(value: float, reference: float | None) -> float | None:
    if reference is None or reference <= 0:
        return None
    return (value / reference - 1.0) * 100.0


def _parse_event_date(value: Any) -> date | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            numeric = float(value)
            if numeric > 10_000_000_000:
                numeric /= 1_000.0
            return pd.Timestamp(numeric, unit="s", tz="UTC").date()
        except (OverflowError, OSError, ValueError):
            return None
    try:
        timestamp = pd.Timestamp(str(value))
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp):
        return None
    return timestamp.date()


def evaluate_event_risk(
    info: dict[str, Any] | None,
    *,
    reference_date: date | None = None,
    config: OpportunityScoringConfig = DEFAULT_OPPORTUNITY_CONFIG,
) -> EventRisk:
    """Obtiene el siguiente resultado conocido sin inventar fechas ausentes."""

    config.validate()
    values = info or {}
    today = reference_date or date.today()
    candidates: list[date] = []
    for key in (
        "earningsTimestamp",
        "earningsTimestampStart",
        "earningsTimestampEnd",
        "earningsDate",
    ):
        raw = values.get(key)
        raw_values = raw if isinstance(raw, (list, tuple)) else (raw,)
        for item in raw_values:
            parsed = _parse_event_date(item)
            if parsed is not None and parsed >= today:
                candidates.append(parsed)
    if not candidates:
        return EventRisk(None, None, "Fecha de resultados N/D", False, 0, False)

    earnings_date = min(candidates)
    days_until = (earnings_date - today).days
    if days_until == 0:
        return EventRisk(
            earnings_date,
            0,
            "Resultados hoy · esperar publicación",
            True,
            config.near_event_penalty,
            True,
        )
    if days_until <= config.near_event_days:
        return EventRisk(
            earnings_date,
            days_until,
            f"Resultados en {days_until} día{'s' if days_until != 1 else ''}",
            False,
            config.near_event_penalty,
            True,
        )
    return EventRisk(
        earnings_date,
        days_until,
        f"Resultados en {days_until} días",
        False,
        0,
        True,
    )


def _signal_reference(frame: pd.DataFrame) -> tuple[pd.Timestamp, float]:
    valid = frame.dropna(subset=["close"])
    if valid.empty:
        raise ValueError("No hay cierres válidos para calcular el timing.")
    if "signal_label" not in valid:
        return pd.Timestamp(valid.index[-1]), float(valid["close"].iloc[-1])

    entry_mask = valid["signal_label"].isin({LABEL_BUY, LABEL_STRONG})
    event_mask = entry_mask & ~entry_mask.shift(1, fill_value=False)
    event_positions = np.flatnonzero(event_mask.to_numpy())
    if not len(event_positions):
        return pd.Timestamp(valid.index[-1]), float(valid["close"].iloc[-1])
    position = int(event_positions[-1])
    return pd.Timestamp(valid.index[position]), float(valid["close"].iloc[position])


def _latest_breakout_level(frame: pd.DataFrame, lookback: int = 20) -> float | None:
    if "high" not in frame or len(frame) < 2:
        return None
    prior = pd.to_numeric(frame["high"], errors="coerce").shift(1)
    level = prior.rolling(lookback, min_periods=min(10, lookback)).max().iloc[-1]
    return _number(level)


def calculate_entry_zones(
    frame: pd.DataFrame,
    *,
    breakout_lookback: int = 20,
) -> EntryZones:
    """Calcula rangos desde ATR, medias, soporte, ruptura y swing reciente."""

    required = {"close", "high", "low"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Faltan columnas para las zonas: {', '.join(sorted(missing))}")
    valid = frame.dropna(subset=["close", "high", "low"])
    if valid.empty:
        raise ValueError("No hay precios válidos para calcular zonas de entrada.")
    latest = valid.iloc[-1]
    current = float(latest["close"])
    atr = _number(latest.get("atr_14"))
    if atr is None or atr <= 0:
        recent_range = (
            pd.to_numeric(valid["high"].tail(14), errors="coerce")
            - pd.to_numeric(valid["low"].tail(14), errors="coerce")
        ).median()
        atr = _number(recent_range) or current * 0.02

    breakout = _latest_breakout_level(valid, breakout_lookback)
    sma20 = _number(latest.get("sma_short"))
    sma50 = _number(latest.get("sma_medium"))
    swing_low = _number(pd.to_numeric(valid["low"].tail(10), errors="coerce").min())
    support_20 = _number(pd.to_numeric(valid["low"].tail(20), errors="coerce").min())
    candidates = [sma20, sma50, breakout, swing_low, support_20]
    supports = sorted(
        {
            float(value)
            for value in candidates
            if value is not None and 0 < float(value) <= current
        },
        reverse=True,
    )
    preferred_center = supports[0] if supports else current - atr
    lower_supports = [value for value in supports[1:] if value < preferred_center - 0.5 * atr]
    excellent_center = lower_supports[0] if lower_supports else preferred_center - 1.5 * atr
    excellent_center = max(excellent_center, current * 0.01)

    def zone(
        center: float,
        lower_atr: float,
        upper_atr: float,
        *,
        cap_at_current: bool = False,
    ) -> PriceZone:
        lower = max(0.0001, center - lower_atr * atr)
        upper = max(lower, center + upper_atr * atr)
        if cap_at_current:
            upper = min(upper, current)
            lower = min(lower, upper)
        return PriceZone(round(lower, 2), round(upper, 2))

    aggressive = zone(current - 0.10 * atr, 0.25, 0.20)
    preferred = zone(preferred_center, 0.35, 0.25, cap_at_current=True)
    excellent = zone(excellent_center, 0.35, 0.25, cap_at_current=True)
    invalidation_basis = min(
        preferred_center,
        excellent_center,
        swing_low or excellent_center,
    )
    invalidation = round(max(0.0001, invalidation_basis - 0.75 * atr), 2)
    risk_to_stop = (
        (current - invalidation) / current * 100.0 if invalidation < current else None
    )
    prior_high_252 = _number(
        pd.to_numeric(valid["high"], errors="coerce").shift(1).tail(252).max()
    )
    target = (
        prior_high_252
        if prior_high_252 is not None and prior_high_252 > current + 0.5 * atr
        else current + 2.0 * atr
    )
    risk_reward = None
    if invalidation < current and target > current:
        risk_reward = (target - current) / (current - invalidation)
    basis_parts = ["ATR", "media corta", "media intermedia", "soportes recientes"]
    if breakout is not None:
        basis_parts.append("última ruptura")
    return EntryZones(
        current_price=round(current, 2),
        aggressive_entry=aggressive,
        preferred_entry=preferred,
        excellent_entry=excellent,
        invalidation=invalidation,
        breakout=round(breakout, 2) if breakout is not None else None,
        target=round(target, 2) if target is not None else None,
        risk_to_stop_pct=round(risk_to_stop, 2) if risk_to_stop is not None else None,
        risk_reward=round(risk_reward, 2) if risk_reward is not None else None,
        basis=", ".join(basis_parts),
    )


def evaluate_entry_timing(
    ticker: str,
    frame: pd.DataFrame,
    *,
    event: EventRisk | None = None,
    signal_price: float | None = None,
    signal_date: date | datetime | pd.Timestamp | None = None,
    breakout_lookback: int = 20,
) -> TimingResult:
    """Mide si el precio actual sigue siendo abordable, separado del momentum."""

    valid = frame.dropna(subset=["close"])
    if valid.empty:
        raise ValueError("No hay cierres válidos para calcular el timing.")
    latest = valid.iloc[-1]
    current = float(latest["close"])
    inferred_date, inferred_price = _signal_reference(valid)
    reference_price = float(signal_price) if signal_price and signal_price > 0 else inferred_price
    reference_date = pd.Timestamp(signal_date) if signal_date is not None else inferred_date
    gap_signal = (current / reference_price - 1.0) * 100.0

    previous_close = float(valid["close"].iloc[-2]) if len(valid) >= 2 else current
    latest_open = _number(latest.get("open")) or current
    gap_previous = (latest_open / previous_close - 1.0) * 100.0
    sma20 = _number(latest.get("sma_short"))
    sma50 = _number(latest.get("sma_medium"))
    sma200 = _number(latest.get("sma_long"))
    atr = _number(latest.get("atr_14"))
    atr_pct = atr / current * 100.0 if atr is not None and atr > 0 else None
    breakout = _latest_breakout_level(valid, breakout_lookback)
    distance_sma20 = _pct_distance(current, sma20)
    distance_sma50 = _pct_distance(current, sma50)
    distance_breakout = _pct_distance(current, breakout)
    high20 = _number(pd.to_numeric(valid["high"].tail(20), errors="coerce").max())
    high252 = _number(pd.to_numeric(valid["high"].tail(252), errors="coerce").max())
    distance_high20 = _pct_distance(current, high20)
    distance_high252 = _pct_distance(current, high252)
    rsi = _number(latest.get("rsi"))
    volume_ratio = _number(latest.get("volume_ratio"))
    macd = _number(latest.get("macd"))
    macd_signal = _number(latest.get("macd_signal"))
    returns = {period: _period_return(valid["close"], period) for period in (1, 5, 20, 60)}

    score = 58.0
    positives: list[str] = []
    risks: list[str] = []
    atr_unit = atr_pct if atr_pct is not None and atr_pct > 0 else 2.0

    extension_atr = gap_signal / atr_unit
    if extension_atr <= 0.5:
        score += 10
        positives.append("el precio continúa cerca del nivel que originó la señal")
    elif extension_atr <= 1.5:
        score += 3
    elif extension_atr <= 2.5:
        score -= 10
        risks.append(f"ha avanzado {gap_signal:+.1f}% desde la señal")
    else:
        score -= 22
        risks.append(f"el movimiento desde la señal ya es amplio ({gap_signal:+.1f}%)")

    distance_sma20_atr = (
        distance_sma20 / atr_unit if distance_sma20 is not None else None
    )
    if distance_sma20_atr is not None:
        if -0.5 <= distance_sma20_atr <= 0.75:
            score += 10
            positives.append("cotiza cerca de la media corta")
        elif distance_sma20_atr <= 1.75:
            score += 3
        elif distance_sma20_atr <= 2.75:
            score -= 10
            risks.append("se ha separado de la media corta")
        else:
            score -= 20
            risks.append("está muy alejada de la media corta")

    if rsi is not None:
        if 45 <= rsi <= 68:
            score += 8
            positives.append(f"RSI constructivo ({rsi:.1f})")
        elif rsi > 78:
            score -= 15
            risks.append(f"RSI muy acelerado ({rsi:.1f})")
        elif rsi < 35:
            score -= 10
            risks.append(f"RSI débil ({rsi:.1f})")

    return_5d = returns[5]
    return_20d = returns[20]
    if return_5d is not None and return_5d > 8:
        score -= 8
        risks.append(f"subida rápida de {return_5d:+.1f}% en cinco sesiones")
    elif return_5d is not None and 0 < return_5d <= 5:
        score += 3
    if return_20d is not None and return_20d > 20:
        score -= 10
        risks.append(f"acumula {return_20d:+.1f}% en veinte sesiones")
    elif return_20d is not None and 2 <= return_20d <= 12:
        score += 4
        positives.append("el avance de veinte sesiones es positivo sin ser extremo")

    if volume_ratio is not None:
        if volume_ratio >= 1.2 and bool(latest.get("breakout", False)):
            score += 7
            positives.append(f"ruptura confirmada con volumen {volume_ratio:.1f}x")
        elif volume_ratio < 0.8 and bool(latest.get("breakout", False)):
            score -= 5
            risks.append("la ruptura llega con volumen poco convincente")

    if distance_breakout is not None and distance_breakout > 2 * atr_unit:
        score -= 8
        risks.append("el precio se ha alejado de su nivel de ruptura")
    elif distance_breakout is not None and -0.5 * atr_unit <= distance_breakout <= atr_unit:
        score += 4

    score_before_event = _score(score)
    resolved_event = event or EventRisk(None, None, "Fecha de resultados N/D", False, 0, False)
    adjusted_score = _score(score_before_event - resolved_event.penalty)
    if resolved_event.penalty:
        risks.append(resolved_event.label)

    if resolved_event.blocked:
        chase = CHASE_DO_NOT
    elif adjusted_score >= 72:
        chase = CHASE_REASONABLE
    elif adjusted_score >= 58:
        chase = CHASE_SOMEWHAT_EXTENDED
    elif adjusted_score >= 42:
        chase = CHASE_WAIT_PULLBACK
    else:
        chase = CHASE_DO_NOT
    return TimingResult(
        ticker=ticker.strip().upper(),
        as_of=pd.Timestamp(valid.index[-1]),
        score_before_event=score_before_event,
        score=adjusted_score,
        chase_label=chase,
        signal_price=reference_price,
        signal_date=reference_date,
        gap_from_signal_pct=round(gap_signal, 2),
        gap_from_previous_close_pct=round(gap_previous, 2),
        distance_sma20_pct=round(distance_sma20, 2) if distance_sma20 is not None else None,
        distance_sma50_pct=round(distance_sma50, 2) if distance_sma50 is not None else None,
        distance_breakout_pct=(
            round(distance_breakout, 2) if distance_breakout is not None else None
        ),
        distance_high_20d_pct=(
            round(distance_high20, 2) if distance_high20 is not None else None
        ),
        distance_high_52w_pct=(
            round(distance_high252, 2) if distance_high252 is not None else None
        ),
        return_1d_pct=round(returns[1], 2) if returns[1] is not None else None,
        return_5d_pct=round(returns[5], 2) if returns[5] is not None else None,
        return_20d_pct=round(returns[20], 2) if returns[20] is not None else None,
        return_60d_pct=round(returns[60], 2) if returns[60] is not None else None,
        rsi=round(rsi, 2) if rsi is not None else None,
        atr_pct=round(atr_pct, 2) if atr_pct is not None else None,
        volume_ratio=round(volume_ratio, 2) if volume_ratio is not None else None,
        sma20=round(sma20, 2) if sma20 is not None else None,
        sma50=round(sma50, 2) if sma50 is not None else None,
        sma200=round(sma200, 2) if sma200 is not None else None,
        macd=round(macd, 4) if macd is not None else None,
        macd_signal=round(macd_signal, 4) if macd_signal is not None else None,
        breakout_price=round(breakout, 2) if breakout is not None else None,
        positive_factors=tuple(positives),
        risk_factors=tuple(risks),
    )


def combine_entry_opportunity_score(
    *,
    technical_score: int,
    timing_score: int,
    fundamental_score: int | None,
    fundamental_coverage: int = 0,
    valuation_score: int | None = None,
    valuation_coverage: int = 0,
    relative_score: int | None = None,
    relative_coverage: int = 0,
    risk_score: int | None = None,
    risk_coverage: int = 0,
    risk_reward: float | None = None,
    config: OpportunityScoringConfig = DEFAULT_OPPORTUNITY_CONFIG,
) -> tuple[int, int]:
    """Combina sólo dimensiones disponibles y devuelve score y cobertura."""

    config.validate()
    risk_reward_score = None
    if risk_reward is not None and np.isfinite(risk_reward):
        if risk_reward >= 3:
            risk_reward_score = 95
        elif risk_reward >= 2:
            risk_reward_score = 82
        elif risk_reward >= 1.5:
            risk_reward_score = 68
        elif risk_reward >= 1:
            risk_reward_score = 45
        else:
            risk_reward_score = 20
    components = (
        (technical_score, config.technical_weight, 100),
        (timing_score, config.timing_weight, 100),
        (fundamental_score, config.fundamental_weight, fundamental_coverage),
        (valuation_score, config.valuation_weight, valuation_coverage),
        (relative_score, config.relative_weight, relative_coverage),
        (risk_score, config.risk_weight, risk_coverage),
        (risk_reward_score, config.reward_risk_weight, 100),
    )
    available = [(value, weight) for value, weight, _ in components if value is not None]
    total_weight = sum(weight for _, weight in available)
    if total_weight <= 0:
        raise ValueError("No hay componentes para calcular la oportunidad.")
    result = sum(float(value) * weight for value, weight in available) / total_weight
    confidence = sum(
        weight * max(0, min(int(coverage), 100)) / 100.0
        for value, weight, coverage in components
        if value is not None
    )
    return _score(result), int(round(confidence))


def evaluate_entry_opportunity(
    *,
    ticker: str,
    company_name: str,
    frame: pd.DataFrame,
    signal: SignalResult,
    fundamental_score: int | None,
    fundamental_coverage: int,
    valuation_score: int | None,
    valuation_coverage: int,
    relative_score: int | None,
    relative_coverage: int,
    risk_score: int | None,
    risk_coverage: int,
    info: dict[str, Any] | None = None,
    sector: str = "",
    market: str = "",
    config: OpportunityScoringConfig = DEFAULT_OPPORTUNITY_CONFIG,
) -> EntryOpportunityResult:
    """Construye la lectura completa sin modificar el motor técnico original."""

    valid = frame.dropna(subset=["close"])
    if valid.empty:
        raise ValueError("No hay datos suficientes para evaluar la oportunidad.")
    latest_date = pd.Timestamp(valid.index[-1]).date()
    # Una fecha empresarial actual no debe bloquear una simulación histórica
    # claramente antigua. En ese caso se conserva como contexto no verificado.
    reference_date = date.today() if abs((date.today() - latest_date).days) <= 7 else latest_date
    event = evaluate_event_risk(info, reference_date=reference_date, config=config)
    timing = evaluate_entry_timing(
        ticker,
        valid,
        event=event,
    )
    zones = calculate_entry_zones(valid)
    opportunity_score, confidence = combine_entry_opportunity_score(
        technical_score=signal.score,
        timing_score=timing.score,
        fundamental_score=fundamental_score,
        fundamental_coverage=fundamental_coverage,
        valuation_score=valuation_score,
        valuation_coverage=valuation_coverage,
        relative_score=relative_score,
        relative_coverage=relative_coverage,
        risk_score=risk_score,
        risk_coverage=risk_coverage,
        risk_reward=zones.risk_reward,
        config=config,
    )
    risk_reward_ok = zones.risk_reward is None or zones.risk_reward >= 1.5
    if event.blocked:
        status_code = STATUS_EVENT
        status_label = "🔴 EVENTO / NO ENTRAR"
    elif timing.chase_label == CHASE_DO_NOT:
        status_code = STATUS_EXTENDED
        status_label = "🟠 EXTENDIDA / NO PERSEGUIR"
    elif timing.chase_label == CHASE_WAIT_PULLBACK:
        status_code = STATUS_WAIT_PRICE
        status_label = "🟡 ESPERAR PRECIO"
    elif (
        opportunity_score >= config.minimum_buyable_score
        and timing.score >= config.minimum_buyable_timing
        and signal.score >= 65
        and risk_reward_ok
    ):
        status_code = STATUS_BUYABLE
        status_label = "🟢 COMPRABLE"
    elif timing.chase_label == CHASE_SOMEWHAT_EXTENDED:
        status_code = STATUS_WAIT_PRICE
        status_label = "🟡 ESPERAR MEJOR PRECIO"
    else:
        status_code = STATUS_WAIT_PRICE
        status_label = "🟡 ESPERAR PRECIO"

    positives = list(timing.positive_factors)
    risks = list(timing.risk_factors)
    if fundamental_score is not None:
        (positives if fundamental_score >= 65 else risks).append(
            f"calidad empresarial {fundamental_score}/100"
        )
    else:
        risks.append("la calidad empresarial no tiene datos suficientes")
    if zones.risk_reward is not None:
        (positives if zones.risk_reward >= 1.5 else risks).append(
            f"beneficio/riesgo técnico {zones.risk_reward:.2f}"
        )
    conclusion = {
        STATUS_BUYABLE: "El precio conserva una zona abordable para estudiar una entrada escalonada.",
        STATUS_WAIT_PRICE: "La empresa puede seguir siendo interesante, pero conviene esperar mejor precio.",
        STATUS_EXTENDED: "Buen momentum no compensa perseguir un movimiento ya avanzado.",
        STATUS_EVENT: "El evento cercano domina el riesgo; conviene esperar información nueva.",
    }[status_code]
    explanation = (
        f"{ticker} mantiene score técnico {signal.score}/100 y timing actual "
        f"{timing.score}/100. Oportunidad conjunta {opportunity_score}/100 con "
        f"cobertura {confidence}%. {conclusion} No es una probabilidad de beneficio."
    )
    return EntryOpportunityResult(
        ticker=ticker.strip().upper(),
        company_name=company_name.strip() or ticker.strip().upper(),
        price=float(valid["close"].iloc[-1]),
        technical_score=int(signal.score),
        technical_label=technical_score_label(signal.score),
        timing=timing,
        fundamental_score=fundamental_score,
        opportunity_score=opportunity_score,
        confidence_pct=confidence,
        status_code=status_code,
        status_label=status_label,
        event=event,
        zones=zones,
        sector=sector.strip() or "Sin sector",
        market=market.strip() or "Mercado N/D",
        position_action=signal.position_label,
        explanation=explanation,
        positive_factors=tuple(positives),
        risk_factors=tuple(risks),
    )


def sector_concentrations(
    opportunities: Iterable[EntryOpportunityResult],
    *,
    minimum_count: int = 2,
) -> dict[str, tuple[str, ...]]:
    """Agrupa señales correlacionadas por sector sin penalizar cada empresa."""

    if minimum_count < 2:
        raise ValueError("La concentración requiere al menos dos empresas.")
    grouped: dict[str, list[str]] = {}
    for result in opportunities:
        sector = result.sector.strip()
        if not sector or sector == "Sin sector":
            continue
        grouped.setdefault(sector, []).append(result.ticker)
    return {
        sector: tuple(dict.fromkeys(tickers))
        for sector, tickers in grouped.items()
        if len(dict.fromkeys(tickers)) >= minimum_count
    }
