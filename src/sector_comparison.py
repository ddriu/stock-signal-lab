"""Comparación homogénea de varias acciones sin mezclar precios nominales."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


HORIZON_SESSIONS = {
    "1 mes": 21,
    "3 meses": 63,
    "6 meses": 126,
    "1 año": 252,
}


@dataclass(frozen=True)
class SectorComparison:
    normalized_prices: pd.DataFrame
    metrics: pd.DataFrame
    correlations: pd.DataFrame
    horizon_label: str
    sessions: int


def _clean_close(frame: pd.DataFrame) -> pd.Series:
    if frame.empty or "close" not in frame:
        return pd.Series(dtype=float)
    close = pd.to_numeric(frame["close"], errors="coerce").dropna()
    close = close.loc[~close.index.duplicated(keep="last")].sort_index()
    return close.loc[close > 0]


def _period_return(close: pd.Series, sessions: int) -> float | None:
    if len(close) <= sessions:
        return None
    return (float(close.iloc[-1]) / float(close.iloc[-sessions - 1]) - 1.0) * 100.0


def _max_drawdown(close: pd.Series) -> float | None:
    if close.empty:
        return None
    drawdown = close / close.cummax() - 1.0
    return float(drawdown.min() * 100.0)


def _peer_percentile_score(metrics: pd.DataFrame) -> pd.Series:
    """Mide liderazgo sólo dentro de las empresas seleccionadas."""

    components = {
        "return_3m_pct": 0.45,
        "return_6m_pct": 0.25,
        "return_1y_pct": 0.15,
        "max_drawdown_1y_pct": 0.15,
    }
    weighted = pd.Series(0.0, index=metrics.index)
    available = pd.Series(0.0, index=metrics.index)
    for column, weight in components.items():
        values = pd.to_numeric(metrics[column], errors="coerce")
        valid = values.notna()
        if not valid.any():
            continue
        percentiles = values.loc[valid].rank(method="average", pct=True)
        weighted.loc[valid] += percentiles * weight
        available.loc[valid] += weight
    result = weighted.div(available.where(available > 0)).mul(100)
    return result.round().clip(0, 100)


def compare_sector(
    frames: dict[str, pd.DataFrame],
    tickers: list[str],
    *,
    horizon_label: str = "6 meses",
) -> SectorComparison:
    """Normaliza precios, resume riesgo/rentabilidad y calcula correlaciones."""

    sessions = HORIZON_SESSIONS.get(horizon_label)
    if sessions is None:
        raise ValueError("Horizonte de comparación no reconocido.")
    selected = list(dict.fromkeys(ticker.strip().upper() for ticker in tickers if ticker.strip()))
    if len(selected) < 2:
        raise ValueError("Selecciona al menos dos empresas para compararlas.")
    if len(selected) > 10:
        raise ValueError("El comparador admite hasta diez empresas a la vez.")

    closes = {
        ticker: _clean_close(frames.get(ticker, pd.DataFrame()))
        for ticker in selected
    }
    closes = {ticker: close for ticker, close in closes.items() if not close.empty}
    if len(closes) < 2:
        raise ValueError("No hay precios válidos de al menos dos empresas.")

    horizon_closes = {
        ticker: close.tail(sessions + 1)
        for ticker, close in closes.items()
    }
    common_start = max(close.index.min() for close in horizon_closes.values())
    normalized_series: dict[str, pd.Series] = {}
    return_series: dict[str, pd.Series] = {}
    rows: list[dict[str, float | str | None]] = []
    for ticker, close in closes.items():
        visible = close.loc[close.index >= common_start].tail(sessions + 1)
        if visible.empty:
            continue
        normalized_series[ticker] = visible / float(visible.iloc[0]) * 100.0
        horizon_returns = visible.pct_change(fill_method=None).dropna()
        return_series[ticker] = horizon_returns
        annualized_volatility = (
            float(horizon_returns.std(ddof=1) * np.sqrt(252) * 100.0)
            if len(horizon_returns) >= 2
            else None
        )
        one_year = close.tail(253)
        rows.append(
            {
                "ticker": ticker,
                "horizon_return_pct": (
                    (float(visible.iloc[-1]) / float(visible.iloc[0]) - 1.0) * 100.0
                ),
                "return_1m_pct": _period_return(close, 21),
                "return_3m_pct": _period_return(close, 63),
                "return_6m_pct": _period_return(close, 126),
                "return_1y_pct": _period_return(close, 252),
                "annualized_volatility_pct": annualized_volatility,
                "max_drawdown_pct": _max_drawdown(visible),
                "max_drawdown_1y_pct": _max_drawdown(one_year),
                "distance_high_pct": (
                    (float(close.iloc[-1]) / float(one_year.max()) - 1.0) * 100.0
                    if not one_year.empty
                    else None
                ),
            }
        )

    if len(normalized_series) < 2:
        raise ValueError("Las empresas no tienen un periodo común suficiente.")
    normalized = pd.concat(normalized_series, axis=1).sort_index()
    metrics = pd.DataFrame(rows).set_index("ticker")
    metrics["leadership_score"] = _peer_percentile_score(metrics)
    metrics = metrics.sort_values(
        ["leadership_score", "horizon_return_pct"],
        ascending=False,
        na_position="last",
    )
    correlations = pd.concat(return_series, axis=1).corr(min_periods=10)
    return SectorComparison(
        normalized_prices=normalized,
        metrics=metrics,
        correlations=correlations,
        horizon_label=horizon_label,
        sessions=sessions,
    )
