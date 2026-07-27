"""Backtest long-only con ejecución diferida y gestión básica del riesgo."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import BacktestConfig, StrategyConfig
from src.signal_engine import add_signal_columns


@dataclass(frozen=True)
class BacktestResult:
    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    metrics: dict[str, float | int]


def _position_allocation(strategy: StrategyConfig) -> float:
    """Convierte riesgo monetario/stop en fracción máxima de capital (capada al 100%)."""

    return min(1.0, strategy.max_risk_per_trade_pct / strategy.stop_loss_pct)


def run_backtest(
    frame: pd.DataFrame,
    strategy: StrategyConfig,
    settings: BacktestConfig,
) -> BacktestResult:
    """Simula entradas al siguiente open y stops intradía sin anticipar el futuro.

    Cuando una vela abre por debajo del stop, la salida se ejecuta en el open; de
    lo contrario, en el nivel del stop. Esto modela de forma explícita el riesgo
    de gap, aunque no sustituye datos intradía.
    """

    strategy.validate()
    settings.validate()
    data = add_signal_columns(frame, strategy) if "signal_score" not in frame else frame.copy()
    data = data.dropna(subset=["open", "high", "low", "close"]).sort_index()
    if len(data) < strategy.sma_long + 2:
        raise ValueError(f"Se necesitan al menos {strategy.sma_long + 2} sesiones para el backtest.")

    cost_rate = (settings.commission_pct + settings.slippage_pct) / 100.0
    cash = settings.initial_capital
    shares = 0.0
    entry_price = 0.0
    entry_total = 0.0
    entry_date: pd.Timestamp | None = None
    peak_price = 0.0
    pending_action: str | None = None
    rows: list[dict[str, float | str | pd.Timestamp]] = []
    trades: list[dict[str, float | str | pd.Timestamp]] = []

    for timestamp, row in data.iterrows():
        timestamp = pd.Timestamp(timestamp)
        open_price = float(row["open"])
        stopped_today = False

        if pending_action == "exit" and shares > 0:
            exit_price = open_price * (1 - cost_rate)
            proceeds = shares * exit_price
            cash += proceeds
            trades.append(
                _trade_record(entry_date, timestamp, entry_price, exit_price, shares, entry_total, proceeds, "Señal")
            )
            shares = 0.0
            entry_price = entry_total = peak_price = 0.0
            entry_date = None
        elif pending_action == "entry" and shares == 0:
            allocation = _position_allocation(strategy)
            budget = cash * allocation
            fill_price = open_price * (1 + cost_rate)
            shares = budget / fill_price
            entry_price = fill_price
            entry_total = shares * fill_price
            cash -= entry_total
            entry_date = timestamp
            # El máximo de la vela aún no existe en el instante de entrada.
            peak_price = open_price
        pending_action = None

        if shares > 0:
            hard_stop = entry_price * (1 - strategy.stop_loss_pct / 100.0)
            trailing_stop = (
                peak_price * (1 - strategy.trailing_stop_pct / 100.0)
                if strategy.trailing_stop_pct > 0
                else -np.inf
            )
            active_stop = max(hard_stop, trailing_stop)
            if float(row["low"]) <= active_stop:
                raw_fill = min(open_price, active_stop)
                exit_price = raw_fill * (1 - cost_rate)
                proceeds = shares * exit_price
                cash += proceeds
                reason = "Trailing stop" if trailing_stop >= hard_stop else "Stop loss"
                trades.append(
                    _trade_record(
                        entry_date, timestamp, entry_price, exit_price, shares, entry_total, proceeds, reason
                    )
                )
                shares = 0.0
                entry_price = entry_total = peak_price = 0.0
                entry_date = None
                stopped_today = True
            else:
                # El nuevo máximo sólo puede endurecer el trailing desde la
                # próxima vela: con OHLC diario no conocemos el orden high/low.
                peak_price = max(peak_price, float(row["high"]))

        equity = cash + shares * float(row["close"])
        rows.append(
            {
                "date": timestamp,
                "equity": equity,
                "cash": cash,
                "position_value": shares * float(row["close"]),
                "in_market": float(shares > 0),
            }
        )

        # La señal del cierre sólo puede ejecutarse en la apertura siguiente.
        if shares == 0 and not stopped_today and bool(row["buy_setup"]):
            pending_action = "entry"
        elif shares > 0:
            exit_signal = bool(row["sell_setup"])
            if strategy.exit_on_reduce:
                exit_signal = exit_signal or bool(row["reduce_setup"])
            if exit_signal:
                pending_action = "exit"

    curve = pd.DataFrame(rows).set_index("date")
    first_close = float(data["close"].iloc[0])
    curve["buy_hold"] = settings.initial_capital * data["close"] / first_close
    curve["running_peak"] = curve["equity"].cummax()
    curve["drawdown"] = curve["equity"] / curve["running_peak"] - 1.0
    trade_frame = pd.DataFrame(trades)
    metrics = _calculate_metrics(curve, trade_frame, settings.initial_capital)
    return BacktestResult(curve, trade_frame, metrics)


def _trade_record(
    entry_date: pd.Timestamp | None,
    exit_date: pd.Timestamp,
    entry_price: float,
    exit_price: float,
    shares: float,
    entry_total: float,
    proceeds: float,
    reason: str,
) -> dict[str, float | str | pd.Timestamp]:
    pnl = proceeds - entry_total
    return {
        "entry_date": entry_date if entry_date is not None else exit_date,
        "exit_date": exit_date,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "quantity": shares,
        "pnl": pnl,
        "return_pct": (pnl / entry_total * 100.0) if entry_total else 0.0,
        "exit_reason": reason,
    }


def _calculate_metrics(
    curve: pd.DataFrame, trades: pd.DataFrame, initial_capital: float
) -> dict[str, float | int]:
    final_equity = float(curve["equity"].iloc[-1])
    total_return = (final_equity / initial_capital - 1.0) * 100.0
    buy_hold_return = (float(curve["buy_hold"].iloc[-1]) / initial_capital - 1.0) * 100.0
    max_drawdown = float(curve["drawdown"].min()) * 100.0
    completed = len(trades)
    win_rate = float((trades["pnl"] > 0).mean() * 100.0) if completed else 0.0
    daily_returns = curve["equity"].pct_change().dropna()
    volatility = float(daily_returns.std() * np.sqrt(252) * 100.0) if len(daily_returns) > 1 else 0.0
    sharpe = (
        float(daily_returns.mean() / daily_returns.std() * np.sqrt(252))
        if len(daily_returns) > 1 and daily_returns.std() > 0
        else 0.0
    )
    exposure = float(curve["in_market"].mean() * 100.0)
    return {
        "final_equity": final_equity,
        "total_return_pct": total_return,
        "buy_hold_return_pct": buy_hold_return,
        "max_drawdown_pct": max_drawdown,
        "completed_trades": completed,
        "win_rate_pct": win_rate,
        "annualized_volatility_pct": volatility,
        "sharpe_zero_rate": sharpe,
        "market_exposure_pct": exposure,
    }
