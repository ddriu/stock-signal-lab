"""Motor explicable de puntuación y clasificación de señales."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import StrategyConfig


LABEL_BUY = "Entrada interesante"
LABEL_STRONG = "Entrada fuerte"
LABEL_WATCH = "Vigilancia"
LABEL_WAIT = "Esperar"
LABEL_HOLD = "Mantener"
LABEL_REDUCE = "Reducir"
LABEL_SELL = "Vender"


@dataclass(frozen=True)
class SignalResult:
    ticker: str
    as_of: pd.Timestamp
    score: int
    label: str
    position_label: str
    explanation: str
    positive_factors: tuple[str, ...]
    risk_factors: tuple[str, ...]


def add_signal_columns(frame: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    """Añade score, condiciones de reglas y etiqueta a todo el histórico."""

    data = frame.copy()
    required = {
        "close",
        "sma_short",
        "sma_medium",
        "sma_long",
        "sma_medium_slope",
        "rsi",
        "macd",
        "macd_signal",
        "macd_bullish_cross",
        "volume_above_average",
        "volume_ratio",
        "distance_sma_short_pct",
        "momentum_short_pct",
        "momentum_medium_pct",
        "macd_hist_rising",
        "breakout",
        "distance_high_pct",
    }
    missing = required.difference(data.columns)
    if missing:
        raise ValueError(f"Calcula primero los indicadores. Faltan: {', '.join(sorted(missing))}")

    conditions = {
        "above_medium": data["close"] > data["sma_medium"],
        "above_long": data["close"] > data["sma_long"],
        "medium_rising": data["sma_medium_slope"] > 0,
        "rsi_constructive": data["rsi"].between(config.rsi_buy_min, config.rsi_buy_max),
        "macd_bullish": data["macd"] > data["macd_signal"],
        "macd_cross": data["macd_bullish_cross"].fillna(False),
        "short_momentum": data["momentum_short_pct"] > 0,
        "medium_momentum": data["momentum_medium_pct"] > 0,
        "breakout": data["breakout"].fillna(False),
        "near_high": data["distance_high_pct"] >= -config.near_high_pct,
        "volume_normal": data["volume_ratio"] >= config.volume_normal_ratio,
        "volume_surge": data["volume_ratio"] >= config.volume_surge_ratio,
        "entry_not_extended": data["distance_sma_short_pct"].abs()
        <= config.distance_from_sma20_pct,
    }
    weights = {
        # Tendencia: 30 puntos
        "above_medium": 10,
        "above_long": 15,
        "medium_rising": 5,
        # Momentum persistente: 30 puntos
        "short_momentum": 8,
        "medium_momentum": 10,
        "macd_bullish": 8,
        "macd_cross": 4,
        # Liderazgo y ruptura: 25 puntos
        "breakout": 10,
        "near_high": 9,
        "volume_normal": 3,
        "volume_surge": 3,
        # Calidad de entrada: 15 puntos
        "rsi_constructive": 10,
        "entry_not_extended": 5,
    }
    score = pd.Series(0.0, index=data.index)
    for name, condition in conditions.items():
        score = score.add(condition.fillna(False).astype(float) * weights[name], fill_value=0.0)
    data["signal_score"] = score.clip(0, 100).round().astype(int)

    below_medium = data["close"] < data["sma_medium"]
    below_long = data["close"] < data["sma_long"]
    below_medium_confirmed = (
        below_medium.astype(int)
        .rolling(config.trend_confirmation_days, min_periods=config.trend_confirmation_days)
        .sum()
        >= config.trend_confirmation_days
    )
    below_long_confirmed = (
        below_long.astype(int)
        .rolling(config.trend_confirmation_days, min_periods=config.trend_confirmation_days)
        .sum()
        >= config.trend_confirmation_days
    )

    data["buy_setup"] = (
        (data["signal_score"] >= config.buy_score_threshold)
        & conditions["above_medium"]
        & conditions["above_long"]
        & conditions["medium_rising"]
        & (conditions["macd_bullish"] | conditions["breakout"])
        & (data["rsi"] <= config.rsi_overbought)
        & (data["distance_sma_short_pct"] <= config.distance_from_sma20_pct)
    ).fillna(False)
    data["strong_setup"] = (
        data["buy_setup"] & (data["signal_score"] >= config.strong_score_threshold)
    ).fillna(False)
    data["watch_setup"] = (data["signal_score"] >= config.watch_score_threshold).fillna(False)
    data["wait_setup"] = (
        (data["rsi"] > config.rsi_overbought)
        | (data["distance_sma_short_pct"] > config.distance_from_sma20_pct)
    ).fillna(False)
    data["sell_setup"] = (
        below_long_confirmed
        | (
            (data["signal_score"] < config.sell_score_threshold)
            & below_medium_confirmed
        )
    ).fillna(False)
    data["reduce_setup"] = (
        below_medium_confirmed
        | (
            (data["signal_score"] < config.reduce_score_threshold)
            & below_medium
        )
    ).fillna(False)

    # La lectura de entrada no llama "mala empresa" a una cotización débil:
    # separa esperar/vigilar de una entrada interesante o fuerte.
    data["signal_label"] = np.select(
        [
            data["wait_setup"],
            data["strong_setup"],
            data["buy_setup"],
            data["watch_setup"],
        ],
        [LABEL_WAIT, LABEL_STRONG, LABEL_BUY, LABEL_WATCH],
        default=LABEL_WAIT,
    )
    # La gestión de una posición existente conserva reglas de salida separadas.
    data["position_label"] = np.select(
        [data["sell_setup"], data["reduce_setup"]],
        [LABEL_SELL, LABEL_REDUCE],
        default=LABEL_HOLD,
    )
    return data


def _describe_latest(row: pd.Series, config: StrategyConfig) -> tuple[list[str], list[str]]:
    positives: list[str] = []
    risks: list[str] = []
    if row["close"] > row["sma_long"]:
        positives.append("el precio conserva la tendencia de largo plazo sobre la media larga")
    else:
        risks.append("el precio está por debajo de la media larga")
    if row["close"] > row["sma_medium"] and row["sma_medium_slope"] > 0:
        positives.append("la media intermedia asciende y el precio cotiza por encima")
    elif row["close"] < row["sma_medium"]:
        risks.append("el precio ha perdido la media intermedia")
    if config.rsi_buy_min <= row["rsi"] <= config.rsi_buy_max:
        positives.append(f"el RSI ({row['rsi']:.1f}) acompaña el movimiento sin sobrecompra extrema")
    elif row["rsi"] > config.rsi_overbought:
        risks.append(f"el RSI ({row['rsi']:.1f}) indica posible sobrecompra")
    elif row["rsi"] < 35:
        risks.append(f"el RSI ({row['rsi']:.1f}) muestra debilidad de momentum")
    if bool(row["macd_bullish_cross"]):
        positives.append("el MACD acaba de cruzar al alza")
    elif row["macd"] > row["macd_signal"]:
        positives.append("el MACD mantiene sesgo alcista, aunque no es un cruce nuevo")
    else:
        risks.append("el MACD no confirma impulso alcista")
    if bool(row["breakout"]):
        positives.append(f"el precio rompe el máximo de las últimas {config.breakout_period} sesiones")
    if row["distance_high_pct"] >= -config.near_high_pct:
        positives.append(
            f"cotiza a un {abs(row['distance_high_pct']):.1f}% de su máximo del periodo, señal de liderazgo"
        )
    if row["momentum_medium_pct"] > 0:
        positives.append(f"el momentum de medio plazo es positivo ({row['momentum_medium_pct']:+.1f}%)")
    else:
        risks.append(f"el momentum de medio plazo es negativo ({row['momentum_medium_pct']:+.1f}%)")
    if row["volume_ratio"] >= config.volume_surge_ratio:
        positives.append(f"el volumen es {row['volume_ratio']:.1f} veces su media reciente")
    elif row["volume_ratio"] >= config.volume_normal_ratio:
        positives.append(
            f"el volumen es suficiente ({row['volume_ratio']:.1f} veces su media), "
            "aunque no especialmente destacado"
        )
    else:
        risks.append("el volumen está por debajo del 80% de su media reciente")
    if row["distance_sma_short_pct"] > config.distance_from_sma20_pct:
        risks.append(
            f"el precio está un {row['distance_sma_short_pct']:.1f}% por encima de la media corta y puede estar extendido"
        )
    return positives, risks


def evaluate_latest_signal(
    frame: pd.DataFrame,
    config: StrategyConfig,
    *,
    ticker: str = "",
    entry_price: float | None = None,
) -> SignalResult:
    """Evalúa la última sesión válida y crea una explicación no prescriptiva."""

    data = add_signal_columns(frame, config) if "signal_score" not in frame else frame.copy()
    valid = data.dropna(subset=["sma_medium", "sma_long", "rsi", "macd_signal"])
    if valid.empty:
        raise ValueError("No hay suficiente histórico para calcular una señal completa.")
    row = valid.iloc[-1].copy()
    label = str(row["signal_label"])
    position_label = str(row["position_label"])
    stop_triggered = False
    if entry_price is not None and entry_price > 0:
        stop_triggered = row["close"] <= entry_price * (1 - config.stop_loss_pct / 100)
        if stop_triggered:
            position_label = LABEL_SELL

    positives, risks = _describe_latest(row, config)
    if stop_triggered:
        risks.insert(0, "el cierre ha alcanzado el stop loss definido desde el precio de entrada")

    symbol = ticker or str(frame.attrs.get("ticker", "Activo"))
    explanation = (
        f"{symbol} obtiene {int(row['signal_score'])}/100 para el momento de entrada "
        f"y se clasifica como «{label}». Para una posición existente: «{position_label}». "
        + ("A favor: " + "; ".join(positives) + ". " if positives else "")
        + ("Riesgos: " + "; ".join(risks) + ". " if risks else "")
        + "Es una lectura probabilística basada en datos históricos, no una recomendación financiera."
    )
    return SignalResult(
        ticker=symbol,
        as_of=pd.Timestamp(valid.index[-1]),
        score=int(row["signal_score"]),
        label=label,
        position_label=position_label,
        explanation=explanation,
        positive_factors=tuple(positives),
        risk_factors=tuple(risks),
    )
