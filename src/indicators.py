"""Indicadores técnicos calculados sin librerías de análisis técnico externas."""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import StrategyConfig


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

    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - previous_close).abs(),
            (data["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    data["atr_14"] = true_range.rolling(14, min_periods=14).mean()
    return data
