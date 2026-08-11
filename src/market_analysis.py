"""Ficha técnica ampliada y explicable para cualquier instrumento.

Este módulo no modifica los pesos del motor de producción. Añade una lectura
desglosada —tendencia, momentum, posición, niveles, entradas y eventos— para
que una misma cifra no oculte por qué el sistema propone esperar o mantener.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

import numpy as np
import pandas as pd

from src.fundamentals import FundamentalResult
from src.opportunity import RelativeStrengthResult, RiskResult, ValuationResult
from src.signal_engine import SignalResult
from src.stop_engine import StopAnalysis, StopConfig, StopMethod, analyze_initial_stop


@dataclass(frozen=True)
class TechnicalLevel:
    label: str
    price: float
    reason: str
    as_of: pd.Timestamp | None
    strength: int


@dataclass(frozen=True)
class EntryOption:
    label: str
    lower: float
    upper: float
    basis: str
    condition: str


@dataclass(frozen=True)
class TargetLevel:
    horizon: str
    price: float
    basis: str


@dataclass(frozen=True)
class AnalysisEvent:
    event_date: date
    label: str
    status: str


@dataclass(frozen=True)
class RecentTechnicalEvent:
    event_date: pd.Timestamp
    label: str
    direction: str


@dataclass(frozen=True)
class ScoreDetail:
    score: int | None
    coverage_pct: int
    positive_factors: tuple[str, ...]
    risk_factors: tuple[str, ...]


@dataclass(frozen=True)
class InstrumentReport:
    ticker: str
    as_of: pd.Timestamp
    price: float
    currency: str
    returns_pct: dict[str, float | None]
    high_52w: float | None
    low_52w: float | None
    distance_high_52w_pct: float | None
    distance_low_52w_pct: float | None
    indicators: dict[str, float | None]
    entry_score: ScoreDetail
    position_score: ScoreDetail
    momentum_score: ScoreDetail
    trend_score: ScoreDetail
    risk_score: ScoreDetail
    quality_score: ScoreDetail
    classification: str
    classification_reason: str
    position_action: str
    supports: tuple[TechnicalLevel, ...]
    resistances: tuple[TechnicalLevel, ...]
    entries: tuple[EntryOption, ...]
    fixed_stop: StopAnalysis
    structural_stop: StopAnalysis
    targets: tuple[TargetLevel, ...]
    events: tuple[AnalysisEvent, ...]
    recent_events: tuple[RecentTechnicalEvent, ...]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _period_return(close: pd.Series, sessions: int) -> float | None:
    values = pd.to_numeric(close, errors="coerce").dropna()
    if len(values) <= sessions:
        return None
    initial = float(values.iloc[-sessions - 1])
    if initial <= 0:
        return None
    return (float(values.iloc[-1]) / initial - 1.0) * 100.0


def _ytd_return(close: pd.Series) -> float | None:
    values = pd.to_numeric(close, errors="coerce").dropna()
    if values.empty:
        return None
    timestamps = pd.to_datetime(values.index)
    current_year = timestamps[-1].year
    current = values.loc[timestamps.year == current_year]
    if current.empty or float(current.iloc[0]) <= 0:
        return None
    return (float(current.iloc[-1]) / float(current.iloc[0]) - 1.0) * 100.0


def calculate_return_windows(frame: pd.DataFrame) -> dict[str, float | None]:
    close = pd.to_numeric(frame["close"], errors="coerce").dropna()
    return {
        "1 semana": _period_return(close, 5),
        "1 mes": _period_return(close, 21),
        "3 meses": _period_return(close, 63),
        "6 meses": _period_return(close, 126),
        "Año actual": _ytd_return(close),
        "1 año": _period_return(close, 252),
    }


def _score_trend(frame: pd.DataFrame) -> ScoreDetail:
    latest = frame.iloc[-1]
    price = float(latest["close"])
    specifications = (
        ("sma_20", 15, "Precio sobre SMA20", "Precio bajo SMA20"),
        ("sma_50", 20, "Precio sobre SMA50", "Precio bajo SMA50"),
        ("sma_100", 15, "Precio sobre SMA100", "Precio bajo SMA100"),
        ("sma_200", 20, "Precio sobre SMA200", "Precio bajo SMA200"),
    )
    score = 0
    available = 0
    positives: list[str] = []
    risks: list[str] = []
    for column, weight, positive, negative in specifications:
        value = _number(latest.get(column))
        if value is None:
            continue
        available += weight
        if price > value:
            score += weight
            positives.append(positive)
        else:
            risks.append(negative)

    for column, weight, label in (
        ("sma_20", 10, "SMA20"),
        ("sma_50", 10, "SMA50"),
    ):
        if column not in frame:
            continue
        series = pd.to_numeric(frame[column], errors="coerce").dropna()
        if len(series) <= 10:
            continue
        available += weight
        if float(series.iloc[-1]) > float(series.iloc[-11]):
            score += weight
            positives.append(f"{label} asciende")
        else:
            risks.append(f"{label} todavía no asciende")

    ema20 = _number(latest.get("ema_20"))
    ema50 = _number(latest.get("ema_50"))
    if ema20 is not None and ema50 is not None:
        available += 10
        if ema20 > ema50:
            score += 10
            positives.append("EMA20 está por encima de EMA50")
        else:
            risks.append("EMA20 permanece por debajo de EMA50")
    normalized = round(score / available * 100) if available else None
    return ScoreDetail(normalized, available, tuple(positives), tuple(risks))


def _score_momentum(frame: pd.DataFrame) -> ScoreDetail:
    latest = frame.iloc[-1]
    positives: list[str] = []
    risks: list[str] = []
    score = 0
    available = 0
    rsi = _number(latest.get("rsi"))
    if rsi is not None:
        available += 20
        if 45 <= rsi <= 68:
            score += 20
            positives.append(f"RSI constructivo ({rsi:.1f})")
        elif 35 <= rsi <= 75:
            score += 12
            positives.append(f"RSI todavía no es extremo ({rsi:.1f})")
        else:
            risks.append(f"RSI en zona extrema o débil ({rsi:.1f})")

    macd = _number(latest.get("macd"))
    signal = _number(latest.get("macd_signal"))
    histogram = (
        pd.to_numeric(frame["macd_hist"], errors="coerce").dropna()
        if "macd_hist" in frame
        else pd.Series(dtype=float)
    )
    if macd is not None and signal is not None:
        available += 20
        if macd > signal:
            score += 20
            positives.append("MACD por encima de su señal")
        else:
            risks.append("MACD por debajo de su señal")
    if len(histogram) >= 4:
        available += 10
        if float(histogram.iloc[-1]) > float(histogram.iloc[-4]):
            score += 10
            positives.append("Histograma MACD mejora en tres sesiones")
        else:
            risks.append("El histograma MACD pierde impulso")

    for sessions, weight, label in (
        (20, 15, "un mes"),
        (63, 20, "tres meses"),
    ):
        value = _period_return(frame["close"], sessions)
        if value is None:
            continue
        available += weight
        if value > 0:
            score += weight
            positives.append(f"Momentum de {label} positivo ({value:+.1f}%)")
        else:
            risks.append(f"Momentum de {label} negativo ({value:+.1f}%)")
    if "breakout" in frame:
        available += 10
        if bool(latest.get("breakout", False)):
            score += 10
            positives.append("Ruptura reciente de máximos")
        else:
            risks.append("Sin ruptura nueva en la última sesión")
    volume = _number(latest.get("volume_ratio"))
    if volume is not None:
        available += 5
        if volume >= 0.8:
            score += 5
            positives.append(f"Volumen suficiente ({volume:.2f}x)")
        else:
            risks.append(f"Volumen reducido ({volume:.2f}x)")
    normalized = round(score / available * 100) if available else None
    return ScoreDetail(normalized, available, tuple(positives), tuple(risks))


def _position_score(
    trend: ScoreDetail,
    momentum: ScoreDetail,
    relative: RelativeStrengthResult,
    risk: RiskResult,
) -> ScoreDetail:
    components = (
        (trend.score, 50, trend.coverage_pct),
        (momentum.score, 25, momentum.coverage_pct),
        (relative.score, 10, relative.coverage_pct),
        (risk.score, 15, risk.coverage_pct),
    )
    usable = [(value, weight) for value, weight, _ in components if value is not None]
    total_weight = sum(weight for _, weight in usable)
    score = (
        round(sum(float(value) * weight for value, weight in usable) / total_weight)
        if total_weight
        else None
    )
    coverage = round(
        sum(weight * max(0, min(coverage, 100)) / 100 for value, weight, coverage in components if value is not None)
    )
    positives = [*trend.positive_factors[:3], *momentum.positive_factors[:2]]
    risks = [*trend.risk_factors[:3], *momentum.risk_factors[:2]]
    if relative.score is not None:
        (positives if relative.score >= 55 else risks).append(
            f"Fortaleza relativa {relative.score}/100"
        )
    if risk.score is not None:
        (positives if risk.score >= 55 else risks).append(
            f"Control de riesgo {risk.score}/100"
        )
    return ScoreDetail(score, coverage, tuple(positives), tuple(risks))


def _level_candidates(frame: pd.DataFrame) -> list[tuple[float, str, pd.Timestamp | None, int]]:
    valid = frame.dropna(subset=["high", "low", "close"]).tail(252)
    if valid.empty:
        return []
    candidates: list[tuple[float, str, pd.Timestamp | None, int]] = []
    for period in (20, 50, 100, 252):
        window = valid.tail(min(period, len(valid)))
        if len(window) < min(10, period):
            continue
        low_index = pd.to_numeric(window["low"], errors="coerce").idxmin()
        high_index = pd.to_numeric(window["high"], errors="coerce").idxmax()
        candidates.append((float(window.loc[low_index, "low"]), f"mínimo de {period} sesiones", pd.Timestamp(low_index), 2))
        candidates.append((float(window.loc[high_index, "high"]), f"máximo de {period} sesiones", pd.Timestamp(high_index), 2))

    latest = valid.iloc[-1]
    for column, label in (
        ("sma_20", "SMA20"),
        ("sma_50", "SMA50"),
        ("sma_100", "SMA100"),
        ("sma_200", "SMA200"),
        ("ema_20", "EMA20"),
        ("ema_50", "EMA50"),
    ):
        value = _number(latest.get(column))
        if value is not None and value > 0:
            candidates.append((value, label, pd.Timestamp(valid.index[-1]), 1))

    lows = pd.to_numeric(valid["low"], errors="coerce").to_numpy(dtype=float)
    highs = pd.to_numeric(valid["high"], errors="coerce").to_numpy(dtype=float)
    window = 3
    for position in range(window, len(valid) - window):
        low = lows[position]
        high = highs[position]
        if np.isfinite(low) and low <= np.nanmin(lows[position - window : position + window + 1]):
            candidates.append((float(low), "mínimo de giro confirmado", pd.Timestamp(valid.index[position]), 3))
        if np.isfinite(high) and high >= np.nanmax(highs[position - window : position + window + 1]):
            candidates.append((float(high), "máximo de giro confirmado", pd.Timestamp(valid.index[position]), 3))

    # Un gap grande suele dejar una zona observada por el mercado. Sólo se usa
    # si supera tanto el 2% como un ATR previo aproximado.
    atr = pd.to_numeric(valid.get("atr_14"), errors="coerce")
    close = pd.to_numeric(valid["close"], errors="coerce")
    opened = pd.to_numeric(valid["open"], errors="coerce")
    for position in range(max(1, len(valid) - 60), len(valid)):
        previous = float(close.iloc[position - 1])
        if previous <= 0:
            continue
        gap_pct = abs(float(opened.iloc[position]) / previous - 1.0) * 100
        atr_pct = (
            float(atr.iloc[position - 1]) / previous * 100
            if position - 1 < len(atr) and np.isfinite(atr.iloc[position - 1])
            else 0.0
        )
        if gap_pct < max(2.0, atr_pct):
            continue
        direction = "alcista" if float(opened.iloc[position]) > previous else "bajista"
        candidates.append(
            (
                float(valid["low"].iloc[position]),
                f"base de gap {direction}",
                pd.Timestamp(valid.index[position]),
                3,
            )
        )
        candidates.append(
            (
                float(valid["high"].iloc[position]),
                f"techo de gap {direction}",
                pd.Timestamp(valid.index[position]),
                3,
            )
        )
    return candidates


def detect_support_resistance(
    frame: pd.DataFrame,
    *,
    levels_per_side: int = 3,
) -> tuple[tuple[TechnicalLevel, ...], tuple[TechnicalLevel, ...]]:
    """Selecciona niveles observables y evita duplicados dentro del ruido ATR."""

    valid = frame.dropna(subset=["close"])
    if valid.empty:
        return (), ()
    current = float(valid["close"].iloc[-1])
    atr = _number(valid.iloc[-1].get("atr_14")) or current * 0.02
    tolerance = max(atr * 0.40, current * 0.004)
    candidates = _level_candidates(valid)

    def choose(side: str) -> tuple[TechnicalLevel, ...]:
        filtered = [
            candidate
            for candidate in candidates
            if (candidate[0] < current if side == "support" else candidate[0] > current)
        ]
        filtered.sort(key=lambda item: item[0], reverse=side == "support")
        chosen: list[tuple[float, str, pd.Timestamp | None, int]] = []
        for candidate in filtered:
            nearby = next(
                (index for index, saved in enumerate(chosen) if abs(candidate[0] - saved[0]) <= tolerance),
                None,
            )
            if nearby is None:
                chosen.append(candidate)
            elif candidate[3] > chosen[nearby][3]:
                chosen[nearby] = candidate
            if len(chosen) >= levels_per_side:
                break
        chosen.sort(key=lambda item: item[0], reverse=side == "support")
        prefix = "S" if side == "support" else "R"

        def contextual_reason(reason: str) -> str:
            # Un máximo ya superado puede actuar como soporte en un retesteo;
            # describirlo simplemente como «máximo» resultaba confuso.
            if side == "support" and reason.startswith("máximo"):
                return f"antigua resistencia / posible soporte ({reason})"
            if side == "support" and reason.startswith("techo de gap"):
                return f"gap superado / posible soporte ({reason})"
            if side == "resistance" and reason.startswith("mínimo"):
                return f"antiguo soporte / posible resistencia ({reason})"
            return reason

        return tuple(
            TechnicalLevel(
                f"{prefix}{index}",
                round(value, 4),
                contextual_reason(reason),
                timestamp,
                strength,
            )
            for index, (value, reason, timestamp, strength) in enumerate(chosen[:levels_per_side], start=1)
        )

    return choose("support"), choose("resistance")


def _entry_options(
    frame: pd.DataFrame,
    supports: tuple[TechnicalLevel, ...],
) -> tuple[EntryOption, ...]:
    latest = frame.iloc[-1]
    current = float(latest["close"])
    atr = _number(latest.get("atr_14")) or current * 0.02
    rsi = _number(latest.get("rsi"))
    ema20 = _number(latest.get("ema_20"))
    sma20 = _number(latest.get("sma_20"))
    sma50 = _number(latest.get("sma_50"))

    nearest = supports[0].price if supports else None
    second = supports[1].price if len(supports) > 1 else None
    aggressive_center = current - 0.10 * atr
    aggressive_condition = "Sólo tras cierre estable; no perseguir un gap o RSI acelerado."
    extended = (rsi is not None and rsi > 68) or (
        ema20 is not None and current > ema20 + atr
    )
    if extended:
        # En un movimiento extendido las tres alternativas deben representar
        # decisiones realmente distintas: retroceso corto, primer soporte y
        # soporte inferior. No se fabrican niveles, se parte de ATR y soportes.
        aggressive_center = min(
            current - 0.15 * atr,
            (nearest + 0.25 * atr) if nearest is not None else current - 0.60 * atr,
        )
        aggressive_condition = "Esperar un retroceso corto y confirmar que la ruptura no se deshace."
    reasonable_center = nearest or sma20 or current - atr
    lower_candidates = [value for value in (second, sma50, sma20) if value is not None and value < reasonable_center - 0.30 * atr]
    optimal_center = max(lower_candidates) if lower_candidates else reasonable_center - 1.25 * atr

    centers = [
        ("Agresiva", aggressive_center, "precio/EMA20 y volatilidad", aggressive_condition),
        ("Razonable", reasonable_center, supports[0].reason if supports else "SMA20 y ATR", "Exigir rebote o estabilización sobre el primer soporte."),
        ("Óptima", optimal_center, supports[1].reason if len(supports) > 1 else "segundo soporte o SMA50", "Sólo si la tesis sigue intacta y el mercado recupera la zona."),
    ]
    result: list[EntryOption] = []
    for label, center, basis, condition in centers:
        lower = max(0.0001, center - 0.18 * atr)
        upper = min(current, center + 0.18 * atr)
        if upper < lower:
            lower = upper
        result.append(EntryOption(label, round(lower, 4), round(upper, 4), basis, condition))
    return tuple(result)


def _target_levels(frame: pd.DataFrame) -> tuple[TargetLevel, ...]:
    """Elige objetivos observados y suficientemente separados en volatilidad.

    Los tres horizontes no deben ser tres máximos casi idénticos. Se agrupan
    niveles dentro del ruido de mercado y se exige más separación a medida que
    aumenta el horizonte. Si el histórico no ofrece tres referencias reales,
    se muestran menos en vez de inventarlas.
    """

    valid = frame.dropna(subset=["close"])
    if valid.empty:
        return ()
    current = float(valid["close"].iloc[-1])
    atr = _number(valid.iloc[-1].get("atr_14")) or current * 0.02
    candidates = [item for item in _level_candidates(valid) if item[0] > current]
    candidates.sort(key=lambda item: item[0])

    clustered: list[tuple[float, str, pd.Timestamp | None, int]] = []
    tolerance = max(0.35 * atr, 0.004 * current)
    for candidate in candidates:
        if clustered and abs(candidate[0] - clustered[-1][0]) <= tolerance:
            if candidate[3] > clustered[-1][3]:
                clustered[-1] = candidate
            continue
        clustered.append(candidate)

    selected: list[tuple[float, str, pd.Timestamp | None, int]] = []
    minimum_gaps = (0.40 * atr, 0.85 * atr, 1.50 * atr)
    anchor = current
    for gap in minimum_gaps:
        choice = next((item for item in clustered if item[0] >= anchor + gap), None)
        if choice is None:
            break
        selected.append(choice)
        anchor = choice[0]
        clustered = [item for item in clustered if item[0] > anchor]

    horizons = ("3–6 meses", "6–12 meses", "12–24 meses")
    return tuple(
        TargetLevel(horizon, round(level[0], 4), level[1])
        for horizon, level in zip(horizons, selected)
    )


def _parse_date(value: Any) -> date | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            number = float(value)
            if number > 10_000_000_000:
                number /= 1_000
            return pd.Timestamp(number, unit="s", tz="UTC").date()
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = pd.Timestamp(str(value))
    except (TypeError, ValueError):
        return None
    return None if pd.isna(parsed) else parsed.date()


def extract_company_events(
    info: dict[str, Any],
    *,
    reference_date: date,
) -> tuple[AnalysisEvent, ...]:
    specifications = (
        ("earningsTimestamp", "Resultados"),
        ("earningsTimestampStart", "Inicio estimado de resultados"),
        ("earningsTimestampEnd", "Fin estimado de resultados"),
        ("earningsDate", "Resultados"),
        ("exDividendDate", "Fecha ex-dividendo"),
        ("dividendDate", "Pago de dividendo"),
    )
    events: dict[tuple[date, str], AnalysisEvent] = {}
    for key, label in specifications:
        raw = info.get(key)
        values: Iterable[Any] = raw if isinstance(raw, (list, tuple)) else (raw,)
        for value in values:
            parsed = _parse_date(value)
            if parsed is None:
                continue
            status = "Próximo" if parsed >= reference_date else "Pasado"
            events[(parsed, label)] = AnalysisEvent(parsed, label, status)
    return tuple(
        sorted(events.values(), key=lambda item: (item.status != "Próximo", item.event_date))
    )


def detect_recent_technical_events(
    frame: pd.DataFrame,
    *,
    lookback: int = 60,
) -> tuple[RecentTechnicalEvent, ...]:
    data = frame.tail(max(lookback + 2, 5)).copy()
    if data.empty:
        return ()
    events: list[RecentTechnicalEvent] = []

    def add_cross(left: str, right: str, bullish: str, bearish: str) -> None:
        if left not in data or right not in data:
            return
        difference = pd.to_numeric(data[left], errors="coerce") - pd.to_numeric(data[right], errors="coerce")
        up = (difference > 0) & (difference.shift(1) <= 0)
        down = (difference < 0) & (difference.shift(1) >= 0)
        for timestamp in data.index[up.fillna(False)]:
            events.append(RecentTechnicalEvent(pd.Timestamp(timestamp), bullish, "positive"))
        for timestamp in data.index[down.fillna(False)]:
            events.append(RecentTechnicalEvent(pd.Timestamp(timestamp), bearish, "negative"))

    add_cross("macd", "macd_signal", "Cruce alcista de MACD", "Cruce bajista de MACD")
    add_cross("close", "sma_50", "Recupera SMA50", "Pierde SMA50")
    add_cross("close", "sma_200", "Recupera SMA200", "Pierde SMA200")
    add_cross("ema_20", "ema_50", "Cruce alcista EMA20/EMA50", "Cruce bajista EMA20/EMA50")

    if "breakout" in data:
        for timestamp in data.index[data["breakout"].fillna(False)]:
            events.append(RecentTechnicalEvent(pd.Timestamp(timestamp), "Ruptura de máximo reciente", "positive"))
    close = pd.to_numeric(data.get("close"), errors="coerce")
    opened = pd.to_numeric(data.get("open"), errors="coerce")
    atr = pd.to_numeric(data.get("atr_14"), errors="coerce")
    for position in range(1, len(data)):
        previous = float(close.iloc[position - 1])
        if previous <= 0 or not np.isfinite(opened.iloc[position]):
            continue
        gap = (float(opened.iloc[position]) / previous - 1.0) * 100
        atr_pct = float(atr.iloc[position - 1]) / previous * 100 if np.isfinite(atr.iloc[position - 1]) else 0
        if abs(gap) >= max(2.0, atr_pct):
            events.append(
                RecentTechnicalEvent(
                    pd.Timestamp(data.index[position]),
                    f"Gap {'alcista' if gap > 0 else 'bajista'} de {gap:+.1f}%",
                    "positive" if gap > 0 else "negative",
                )
            )
    unique: dict[tuple[pd.Timestamp, str], RecentTechnicalEvent] = {
        (event.event_date, event.label): event for event in events
    }
    return tuple(sorted(unique.values(), key=lambda event: event.event_date, reverse=True)[:8])


def build_instrument_report(
    *,
    ticker: str,
    frame: pd.DataFrame,
    info: dict[str, Any],
    signal: SignalResult,
    fundamentals: FundamentalResult,
    valuation: ValuationResult,
    relative: RelativeStrengthResult,
    risk: RiskResult,
    fixed_stop_pct: float = 8.0,
) -> InstrumentReport:
    """Construye la ficha sin modificar ninguna señal o peso de producción."""

    required = {"open", "high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Faltan columnas para el informe: {', '.join(sorted(missing))}")
    valid = frame.dropna(subset=["open", "high", "low", "close"]).sort_index()
    if len(valid) < 20:
        raise ValueError("Se necesitan al menos 20 sesiones para crear la ficha ampliada.")
    latest = valid.iloc[-1]
    current = float(latest["close"])
    returns = calculate_return_windows(valid)
    high_52 = _number(pd.to_numeric(valid["high"].tail(252), errors="coerce").max())
    low_52 = _number(pd.to_numeric(valid["low"].tail(252), errors="coerce").min())
    trend = _score_trend(valid)
    momentum = _score_momentum(valid)
    position = _position_score(trend, momentum, relative, risk)
    quality = ScoreDetail(
        fundamentals.score,
        fundamentals.coverage_pct,
        fundamentals.positive_factors,
        fundamentals.risk_factors,
    )
    entry = ScoreDetail(signal.score, 100, signal.positive_factors, signal.risk_factors)
    risk_detail = ScoreDetail(risk.score, risk.coverage_pct, risk.positive_factors, risk.risk_factors)

    one_year = returns["1 año"]
    quality_ok = fundamentals.score is not None and fundamentals.score >= 60 and fundamentals.coverage_pct >= 50
    value_ok = valuation.score is not None and valuation.score >= 55
    reversal_ok = (
        one_year is not None
        and one_year < 0
        and (trend.score or 0) >= 65
        and (momentum.score or 0) >= 55
    )
    if quality_ok and value_ok and reversal_ok:
        classification = "QUALITY_TURNAROUND"
        classification_reason = (
            "Calidad y valoración suficientes, caída anual previa y recuperación técnica "
            "objetiva. Describe un estado medible; no garantiza que el giro continúe."
        )
    elif quality_ok and reversal_ok:
        classification = "QUALITY_TURNAROUND_WATCH"
        classification_reason = "Hay calidad y giro técnico, pero la valoración no está suficientemente cubierta o no es favorable."
    elif reversal_ok:
        classification = "TURNAROUND_WATCH"
        classification_reason = "El precio mejora tras un año débil, pero falta confirmar calidad empresarial."
    elif (trend.score or 0) >= 70 and (momentum.score or 0) >= 65:
        classification = "MOMENTUM"
        classification_reason = "Tendencia y momentum son positivos sin evidencia completa de turnaround de calidad."
    elif quality_ok:
        classification = "QUALITY"
        classification_reason = "La empresa supera el filtro de calidad, pero no confirma todavía un giro técnico completo."
    else:
        classification = "SIN_CLASIFICAR"
        classification_reason = "Los datos no activan de forma conjunta los filtros objetivos disponibles."

    sma50 = _number(latest.get("sma_50"))
    sma200 = _number(latest.get("sma_200"))
    if sma200 is not None and current < sma200:
        position_action = "Revisar / reducir"
    elif position.score is not None and position.score >= 70:
        position_action = "Mantener"
    elif sma50 is not None and current < sma50:
        position_action = "Reducir si no recupera SMA50"
    elif position.score is not None and position.score < 45:
        position_action = "Reducir"
    else:
        position_action = "Mantener con vigilancia"

    supports, resistances = detect_support_resistance(valid)
    entries = _entry_options(valid, supports)
    fixed = analyze_initial_stop(
        valid,
        entry_price=current,
        config=StopConfig(method=StopMethod.FIXED, fixed_stop_pct=fixed_stop_pct),
    )
    structural = analyze_initial_stop(
        valid,
        entry_price=current,
        config=StopConfig(method=StopMethod.STRUCTURAL, fixed_stop_pct=fixed_stop_pct),
    )
    targets = _target_levels(valid)
    events = extract_company_events(info, reference_date=pd.Timestamp(valid.index[-1]).date())
    indicators = {
        key: _number(latest.get(key))
        for key in (
            "sma_20",
            "sma_50",
            "sma_100",
            "sma_200",
            "ema_20",
            "ema_50",
            "rsi",
            "macd",
            "macd_signal",
            "macd_hist",
            "atr_14",
            "adx_14",
            "plus_di_14",
            "minus_di_14",
            "volume_ratio",
        )
    }
    return InstrumentReport(
        ticker=ticker.strip().upper(),
        as_of=pd.Timestamp(valid.index[-1]),
        price=current,
        currency=str(valid.attrs.get("display_currency") or info.get("currency") or "").upper(),
        returns_pct=returns,
        high_52w=high_52,
        low_52w=low_52,
        distance_high_52w_pct=(current / high_52 - 1) * 100 if high_52 else None,
        distance_low_52w_pct=(current / low_52 - 1) * 100 if low_52 else None,
        indicators=indicators,
        entry_score=entry,
        position_score=position,
        momentum_score=momentum,
        trend_score=trend,
        risk_score=risk_detail,
        quality_score=quality,
        classification=classification,
        classification_reason=classification_reason,
        position_action=position_action,
        supports=supports,
        resistances=resistances,
        entries=entries,
        fixed_stop=fixed,
        structural_stop=structural,
        targets=targets,
        events=events,
        recent_events=detect_recent_technical_events(valid),
    )
