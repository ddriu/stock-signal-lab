"""Motor causal y reutilizable para comparar stops iniciales.

La Fase 1 mantiene ``fixed`` como método por defecto. El módulo no cambia por sí
solo ninguna posición, alerta u orden: calcula niveles reproducibles a partir de
la información disponible al cierre que precede a la entrada.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd


class StopMethod(str, Enum):
    FIXED = "fixed"
    ATR = "atr"
    STRUCTURAL = "structural"


class AtrMethod(str, Enum):
    SMA = "sma"
    WILDER = "wilder"


@dataclass(frozen=True)
class StopConfig:
    """Configuración experimental; ``fixed`` conserva el comportamiento actual."""

    method: StopMethod | str = StopMethod.FIXED
    fixed_stop_pct: float = 8.0
    atr_period: int = 14
    atr_method: AtrMethod | str = AtrMethod.SMA
    atr_multiplier: float = 2.0
    support_lookback: int = 20
    support_buffer_atr: float = 0.50
    swing_window: int = 3
    max_stop_pct: float = 25.0

    def validate(self) -> None:
        try:
            StopMethod(self.method)
        except ValueError as exc:
            raise ValueError("El método debe ser fixed, atr o structural.") from exc
        try:
            AtrMethod(self.atr_method)
        except ValueError as exc:
            raise ValueError("El ATR debe utilizar sma o wilder.") from exc
        if not 0 < self.fixed_stop_pct < 100:
            raise ValueError("El stop fijo debe estar entre 0 y 100%.")
        if self.atr_period < 2 or self.support_lookback < 2:
            raise ValueError("Los periodos de ATR y soporte deben ser al menos 2.")
        if self.atr_multiplier <= 0 or self.support_buffer_atr < 0:
            raise ValueError("Los multiplicadores de ATR no pueden ser negativos.")
        if self.swing_window < 1:
            raise ValueError("La ventana de swing debe ser positiva.")
        if not 0 < self.max_stop_pct < 100:
            raise ValueError("La distancia máxima del stop debe estar entre 0 y 100%.")


@dataclass(frozen=True)
class SupportLevel:
    price: float
    kind: str
    as_of: pd.Timestamp
    age_bars: int


@dataclass(frozen=True)
class StopAnalysis:
    entry_price: float
    atr: float
    atr_pct: float
    atr_method: str
    support: float | None
    support_type: str
    support_age_bars: int | None
    support_distance_pct: float | None
    support_buffer: float
    fixed_stop: float
    atr_stop: float
    structural_stop: float
    recommended_stop: float
    stop_method: str
    stop_distance_pct: float
    stop_distance_atr: float
    reason: str


def true_range(frame: pd.DataFrame) -> pd.Series:
    """True Range diario, incluyendo gaps respecto al cierre anterior."""

    required = {"high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Faltan columnas OHLC: {', '.join(sorted(missing))}")
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    previous_close = close.shift(1)
    return pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def calculate_atr(
    frame: pd.DataFrame,
    *,
    period: int = 14,
    method: AtrMethod | str = AtrMethod.SMA,
) -> pd.Series:
    """Calcula ATR simple —el existente— o el suavizado estándar de Wilder."""

    if period < 2:
        raise ValueError("El periodo ATR debe ser al menos 2.")
    try:
        selected = AtrMethod(method)
    except ValueError as exc:
        raise ValueError("El ATR debe utilizar sma o wilder.") from exc
    ranges = true_range(frame)
    if selected == AtrMethod.WILDER:
        atr = ranges.ewm(
            alpha=1.0 / period,
            adjust=False,
            min_periods=period,
        ).mean()
    else:
        atr = ranges.rolling(period, min_periods=period).mean()
    return atr.rename(f"atr_{period}_{selected.value}")


def _bars_since(index: pd.Index, timestamp: object) -> int:
    positions = np.flatnonzero(index == timestamp)
    return int(len(index) - 1 - positions[-1]) if len(positions) else 0


def _confirmed_swing_low(
    frame: pd.DataFrame,
    *,
    window: int,
    entry_price: float,
) -> SupportLevel | None:
    """Último mínimo confirmado; sus barras posteriores ya existen al decidir."""

    lows = pd.to_numeric(frame["low"], errors="coerce")
    if len(lows) < window * 2 + 1:
        return None
    values = lows.to_numpy(dtype=float)
    for position in range(len(values) - window - 1, window - 1, -1):
        value = values[position]
        if not np.isfinite(value) or not 0 < value < entry_price:
            continue
        neighbourhood = values[position - window : position + window + 1]
        if np.isfinite(neighbourhood).any() and value <= np.nanmin(neighbourhood):
            timestamp = pd.Timestamp(frame.index[position])
            return SupportLevel(
                price=float(value),
                kind="swing low confirmado",
                as_of=timestamp,
                age_bars=len(frame) - 1 - position,
            )
    return None


def detect_support_levels(
    frame: pd.DataFrame,
    *,
    entry_price: float,
    lookback: int = 20,
    swing_window: int = 3,
) -> tuple[SupportLevel, ...]:
    """Detecta soportes utilizando exclusivamente el histórico recibido."""

    if frame.empty or entry_price <= 0:
        return ()
    required = {"high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Faltan columnas para soportes: {', '.join(sorted(missing))}")
    valid = frame.dropna(subset=["high", "low", "close"]).sort_index()
    if valid.empty:
        return ()
    candidates: list[SupportLevel] = []

    recent = pd.to_numeric(valid["low"].tail(lookback), errors="coerce")
    if not recent.dropna().empty:
        timestamp = recent.idxmin()
        value = float(recent.loc[timestamp])
        if 0 < value < entry_price:
            candidates.append(
                SupportLevel(
                    value,
                    f"mínimo de {lookback} sesiones",
                    pd.Timestamp(timestamp),
                    _bars_since(valid.index, timestamp),
                )
            )

    swing = _confirmed_swing_low(
        valid,
        window=swing_window,
        entry_price=entry_price,
    )
    if swing is not None:
        candidates.append(swing)

    for column, label in (
        ("sma_short", "media corta"),
        ("sma_medium", "media intermedia"),
    ):
        if column not in valid:
            continue
        value = float(pd.to_numeric(valid[column], errors="coerce").iloc[-1])
        if np.isfinite(value) and 0 < value < entry_price:
            candidates.append(
                SupportLevel(value, label, pd.Timestamp(valid.index[-1]), 0)
            )

    prior_highs = pd.to_numeric(valid["high"].iloc[:-1].tail(lookback), errors="coerce")
    if not prior_highs.dropna().empty:
        timestamp = prior_highs.idxmax()
        value = float(prior_highs.loc[timestamp])
        if 0 < value < entry_price:
            candidates.append(
                SupportLevel(
                    value,
                    "ruptura previa / posible retest",
                    pd.Timestamp(timestamp),
                    _bars_since(valid.index, timestamp),
                )
            )

    unique: list[SupportLevel] = []
    for candidate in sorted(candidates, key=lambda item: item.price, reverse=True):
        if not any(abs(candidate.price - saved.price) <= entry_price * 0.001 for saved in unique):
            unique.append(candidate)
    return tuple(unique)


def analyze_initial_stop(
    frame: pd.DataFrame,
    *,
    entry_price: float,
    config: StopConfig | None = None,
    decision_at: object | None = None,
) -> StopAnalysis:
    """Calcula los tres candidatos sin consultar barras posteriores a la decisión."""

    settings = config or StopConfig()
    settings.validate()
    if entry_price <= 0:
        raise ValueError("El precio de entrada debe ser positivo.")
    history = frame.sort_index()
    if decision_at is not None:
        history = history.loc[:decision_at]
    history = history.dropna(subset=["high", "low", "close"])
    if history.empty:
        raise ValueError("No hay histórico disponible hasta la fecha de decisión.")

    atr_series = calculate_atr(
        history,
        period=settings.atr_period,
        method=settings.atr_method,
    )
    atr_value = float(atr_series.iloc[-1])
    if not np.isfinite(atr_value) or atr_value <= 0:
        # El stop fijo anterior no dependía del ATR. Permitimos que siga
        # funcionando con historiales cortos y reservamos el requisito de 14
        # sesiones para los dos métodos que sí lo necesitan.
        if StopMethod(settings.method) != StopMethod.FIXED:
            raise ValueError("No hay sesiones suficientes para calcular el ATR del stop.")
        ranges = true_range(history).dropna()
        atr_value = float(ranges.mean()) if not ranges.empty else entry_price * 0.01
        if not np.isfinite(atr_value) or atr_value <= 0:
            atr_value = entry_price * 0.01

    fixed_stop = entry_price * (1.0 - settings.fixed_stop_pct / 100.0)
    atr_stop = entry_price - settings.atr_multiplier * atr_value
    buffer = settings.support_buffer_atr * atr_value
    minimum_allowed = entry_price * (1.0 - settings.max_stop_pct / 100.0)
    atr_stop = max(minimum_allowed, atr_stop, 0.0001)
    levels = detect_support_levels(
        history,
        entry_price=entry_price,
        lookback=settings.support_lookback,
        swing_window=settings.swing_window,
    )
    # Preferimos el soporte más cercano que, tras aplicar el buffer, no deje el
    # stop dentro del ruido definido por ATR. Si ninguno cumple, manda ATR.
    useful = [
        level
        for level in levels
        if minimum_allowed <= level.price - buffer <= atr_stop
    ]
    support = useful[0] if useful else None
    support_stop = support.price - buffer if support is not None else atr_stop
    structural_stop = min(atr_stop, support_stop)

    fixed_stop = max(0.0001, fixed_stop)
    structural_stop = max(structural_stop, 0.0001)

    method = StopMethod(settings.method)
    selected_stop = {
        StopMethod.FIXED: fixed_stop,
        StopMethod.ATR: atr_stop,
        StopMethod.STRUCTURAL: structural_stop,
    }[method]
    distance = entry_price - selected_stop
    if distance <= 0:
        raise ValueError("El stop calculado debe quedar por debajo de la entrada.")

    if method == StopMethod.FIXED:
        reason = f"Stop porcentual actual del {settings.fixed_stop_pct:.1f}%."
    elif method == StopMethod.ATR:
        reason = (
            f"Stop a {settings.atr_multiplier:.1f} ATR({settings.atr_period}) "
            f"con suavizado {AtrMethod(settings.atr_method).value.upper()}."
        )
    elif support is None:
        reason = "No apareció un soporte válido; se utiliza el stop ATR."
    else:
        reason = (
            f"Bajo {support.kind} ({support.price:.2f}) con buffer de "
            f"{settings.support_buffer_atr:.2f} ATR, respetando el suelo de ruido ATR."
        )

    return StopAnalysis(
        entry_price=round(entry_price, 6),
        atr=round(atr_value, 6),
        atr_pct=round(atr_value / entry_price * 100.0, 4),
        atr_method=AtrMethod(settings.atr_method).value,
        support=round(support.price, 6) if support is not None else None,
        support_type=support.kind if support is not None else "sin soporte válido",
        support_age_bars=support.age_bars if support is not None else None,
        support_distance_pct=(
            round((entry_price - support.price) / entry_price * 100.0, 4)
            if support is not None
            else None
        ),
        support_buffer=round(buffer, 6),
        fixed_stop=round(fixed_stop, 6),
        atr_stop=round(atr_stop, 6),
        structural_stop=round(structural_stop, 6),
        recommended_stop=round(selected_stop, 6),
        stop_method=method.value,
        stop_distance_pct=round(distance / entry_price * 100.0, 4),
        stop_distance_atr=round(distance / atr_value, 4),
        reason=reason,
    )
