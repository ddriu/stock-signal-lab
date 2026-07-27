"""Configuración compartida por la aplicación y los módulos cuantitativos."""

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyConfig:
    """Parámetros editables de indicadores, señales y gestión del riesgo."""

    sma_short: int = 20
    sma_medium: int = 50
    sma_long: int = 200
    rsi_period: int = 14
    rsi_buy_min: float = 45.0
    rsi_buy_max: float = 68.0
    rsi_overbought: float = 78.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    volume_period: int = 20
    distance_from_sma20_pct: float = 12.0
    momentum_short_period: int = 20
    momentum_medium_period: int = 63
    breakout_period: int = 20
    high_lookback: int = 252
    near_high_pct: float = 12.0
    volume_normal_ratio: float = 0.80
    volume_surge_ratio: float = 1.20
    watch_score_threshold: int = 55
    buy_score_threshold: int = 65
    strong_score_threshold: int = 75
    forward_horizon_days: int = 20
    reduce_score_threshold: int = 40
    sell_score_threshold: int = 25
    trend_confirmation_days: int = 2
    stop_loss_pct: float = 8.0
    trailing_stop_pct: float = 10.0
    max_risk_per_trade_pct: float = 1.0
    exit_on_reduce: bool = True

    def validate(self) -> None:
        if not 1 <= self.sma_short < self.sma_medium < self.sma_long:
            raise ValueError("Las medias deben cumplir: corta < media < larga.")
        if not 0 <= self.rsi_buy_min < self.rsi_buy_max <= 100:
            raise ValueError("La zona favorable de RSI debe ser creciente y estar entre 0 y 100.")
        if not self.rsi_buy_max < self.rsi_overbought <= 100:
            raise ValueError("El umbral de sobrecompra debe superar la zona favorable de RSI.")
        if not 1 <= self.macd_fast < self.macd_slow:
            raise ValueError("MACD rápido debe ser menor que MACD lento.")
        if self.rsi_period < 2 or self.macd_signal < 1 or self.volume_period < 1:
            raise ValueError("Los periodos de indicadores deben ser positivos.")
        if min(
            self.momentum_short_period,
            self.momentum_medium_period,
            self.breakout_period,
            self.high_lookback,
            self.trend_confirmation_days,
            self.forward_horizon_days,
        ) < 1:
            raise ValueError("Los periodos del motor de oportunidades deben ser positivos.")
        if self.momentum_short_period >= self.momentum_medium_period:
            raise ValueError("El momentum corto debe ser menor que el momentum medio.")
        if (
            not 0 < self.near_high_pct < 100
            or self.volume_normal_ratio <= 0
            or self.volume_surge_ratio <= self.volume_normal_ratio
        ):
            raise ValueError("La cercanía a máximos y el ratio de volumen deben ser positivos.")
        if not (
            0
            <= self.sell_score_threshold
            < self.reduce_score_threshold
            < self.watch_score_threshold
            < self.buy_score_threshold
            < self.strong_score_threshold
            <= 100
        ):
            raise ValueError(
                "Los umbrales deben cumplir: vender < reducir < vigilancia "
                "< entrada interesante < entrada fuerte."
            )
        if self.distance_from_sma20_pct <= 0:
            raise ValueError("La distancia máxima a la media debe ser positiva.")
        if not 0 < self.stop_loss_pct < 100:
            raise ValueError("El stop loss debe estar entre 0 y 100%.")
        if not 0 <= self.trailing_stop_pct < 100:
            raise ValueError("El trailing stop debe estar entre 0 y 100%.")
        if not 0 < self.max_risk_per_trade_pct <= 100:
            raise ValueError("El riesgo máximo debe estar entre 0 y 100%.")


@dataclass(frozen=True)
class BacktestConfig:
    """Hipótesis operativas del backtest."""

    initial_capital: float = 10_000.0
    commission_pct: float = 0.10
    slippage_pct: float = 0.05

    def validate(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("El capital inicial debe ser positivo.")
        if self.commission_pct < 0 or self.slippage_pct < 0:
            raise ValueError("Costes y deslizamiento no pueden ser negativos.")
