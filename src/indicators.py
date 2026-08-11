"""Indicadores técnicos calculados sin librerías de análisis técnico externas."""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import StrategyConfig
from src.stop_engine import AtrMethod, calculate_atr


def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI de Wilder mediante medias exponenciales suavizadas."""

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    average_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    average_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    relative_strength = average_gain / average_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + relative_strength))
    rsi = rsi.mask((average_loss == 0) & (average_gain > 0), 100.0)
    rsi = rsi.mask((average_loss == 0) & (average_gain == 0), 50.0)
    return rsi.rename("rsi")


def calculate_adx(frame: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Calcula ADX y sus dos direcciones con el suavizado de Wilder.

    ADX mide fuerza, no dirección: por eso se conservan también ``+DI`` y
    ``-DI``. Los valores iniciales permanecen como N/D hasta reunir historial
    suficiente en lugar de rellenarse con ceros.
    """

    if period < 2:
        raise ValueError("El periodo ADX debe ser al menos 2.")
    required = {"high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Faltan columnas OHLC: {', '.join(sorted(missing))}")

    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    upward = high.diff()
    downward = -low.diff()
    plus_dm = upward.where((upward > downward) & (upward > 0), 0.0)
    minus_dm = downward.where((downward > upward) & (downward > 0), 0.0)
    atr = calculate_atr(frame, period=period, method=AtrMethod.WILDER)
    plus_smoothed = plus_dm.ewm(
        alpha=1 / period, adjust=False, min_periods=period
    ).mean()
    minus_smoothed = minus_dm.ewm(
        alpha=1 / period, adjust=False, min_periods=period
    ).mean()
    plus_di = 100.0 * plus_smoothed / atr.replace(0.0, np.nan)
    minus_di = 100.0 * minus_smoothed / atr.replace(0.0, np.nan)
    denominator = (plus_di + minus_di).replace(0.0, np.nan)
    directional_index = 100.0 * (plus_di - minus_di).abs() / denominator
    adx = directional_index.ewm(
        alpha=1 / period, adjust=False, min_periods=period
    ).mean()
    return pd.DataFrame(
        {"plus_di_14": plus_di, "minus_di_14": minus_di, "adx_14": adx},
        index=frame.index,
    )


def add_indicators(frame: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    """Añade medias, RSI, MACD, volumen medio, ATR y distancia a la media."""

    config.validate()
    if frame.empty:
        return frame.copy()
    required = {"high", "low", "close", "volume"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Faltan columnas OHLCV: {', '.join(sorted(missing))}")

    data = frame.copy()
    close = data["close"].astype(float)
    data["sma_short"] = close.rolling(config.sma_short, min_periods=config.sma_short).mean()
    data["sma_medium"] = close.rolling(config.sma_medium, min_periods=config.sma_medium).mean()
    data["sma_long"] = close.rolling(config.sma_long, min_periods=config.sma_long).mean()
    # Referencias estables para la ficha ampliada. No sustituyen las medias
    # configurables usadas por el score de producción.
    data["sma_20"] = close.rolling(20, min_periods=20).mean()
    data["sma_50"] = close.rolling(50, min_periods=50).mean()
    data["sma_100"] = close.rolling(100, min_periods=100).mean()
    data["sma_200"] = close.rolling(200, min_periods=200).mean()
    data["ema_20"] = close.ewm(span=20, adjust=False, min_periods=20).mean()
    data["ema_50"] = close.ewm(span=50, adjust=False, min_periods=50).mean()
    data["sma_medium_slope"] = data["sma_medium"].diff(5)
    data["rsi"] = calculate_rsi(close, config.rsi_period)

    ema_fast = close.ewm(span=config.macd_fast, adjust=False).mean()
    ema_slow = close.ewm(span=config.macd_slow, adjust=False).mean()
    data["macd"] = ema_fast - ema_slow
    data["macd_signal"] = data["macd"].ewm(span=config.macd_signal, adjust=False).mean()
    data["macd_hist"] = data["macd"] - data["macd_signal"]
    data["macd_bullish_cross"] = (data["macd"] > data["macd_signal"]) & (
        data["macd"].shift(1) <= data["macd_signal"].shift(1)
    )

    data["volume_average"] = data["volume"].rolling(
        config.volume_period, min_periods=config.volume_period
    ).mean()
    data["volume_above_average"] = data["volume"] > data["volume_average"]
    data["volume_ratio"] = data["volume"] / data["volume_average"].replace(0.0, np.nan)
    data["distance_sma_short_pct"] = (close / data["sma_short"] - 1.0) * 100.0

    # Momentum y liderazgo: buscan tendencias persistentes y rupturas, no sólo
    # un evento puntual como el cruce MACD del último día.
    data["momentum_short_pct"] = (
        close.pct_change(config.momentum_short_period, fill_method=None) * 100.0
    )
    data["momentum_medium_pct"] = (
        close.pct_change(config.momentum_medium_period, fill_method=None) * 100.0
    )
    data["macd_hist_rising"] = data["macd_hist"].diff(3) > 0
    prior_breakout_high = close.shift(1).rolling(
        config.breakout_period, min_periods=config.breakout_period
    ).max()
    data["breakout"] = close > prior_breakout_high
    observed_high = close.rolling(
        config.high_lookback,
        min_periods=min(config.sma_long, config.high_lookback),
    ).max()
    data["distance_high_pct"] = (close / observed_high - 1.0) * 100.0

    # Se conserva expresamente el ATR simple que ya utilizaba la aplicación.
    # Wilder queda disponible en el motor de stops para la comparación, pero
    # no cambia todavía ningún score o señal de producción.
    data["atr_14"] = calculate_atr(data, period=14, method=AtrMethod.SMA)
    adx = calculate_adx(data, period=14)
    data = data.join(adx)
    return data
