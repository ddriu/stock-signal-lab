"""Gráficos Plotly reutilizables por la interfaz."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


COLORS = {
    "price": "#E8EDF5",
    "short": "#F5B700",
    "medium": "#3A86FF",
    "long": "#FF4D6D",
    "positive": "#20C997",
    "negative": "#FF6B6B",
    "muted": "#73849A",
}


def price_chart(frame: pd.DataFrame, ticker: str) -> go.Figure:
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.75, 0.25],
    )
    figure.add_trace(
        go.Candlestick(
            x=frame.index,
            open=frame["open"],
            high=frame["high"],
            low=frame["low"],
            close=frame["close"],
            name="Precio",
        ),
        row=1,
        col=1,
    )
    for column, label, color in (
        ("sma_short", "SMA corta", COLORS["short"]),
        ("sma_medium", "SMA media", COLORS["medium"]),
        ("sma_long", "SMA larga", COLORS["long"]),
    ):
        figure.add_trace(
            go.Scatter(x=frame.index, y=frame[column], name=label, line={"color": color, "width": 1.5}),
            row=1,
            col=1,
        )
    volume_colors = [
        COLORS["positive"] if close >= open_ else COLORS["negative"]
        for close, open_ in zip(frame["close"], frame["open"])
    ]
    figure.add_trace(
        go.Bar(x=frame.index, y=frame["volume"], name="Volumen", marker_color=volume_colors),
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=frame.index,
            y=frame["volume_average"],
            name="Volumen medio",
            line={"color": COLORS["muted"], "width": 1},
        ),
        row=2,
        col=1,
    )
    figure.update_layout(
        title=f"{ticker}: precio, medias y volumen",
        height=650,
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        template="plotly_dark",
        legend={"orientation": "h", "y": 1.02},
    )
    return figure


def momentum_chart(frame: pd.DataFrame, overbought: float = 75.0) -> go.Figure:
    figure = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08)
    figure.add_trace(go.Scatter(x=frame.index, y=frame["rsi"], name="RSI"), row=1, col=1)
    figure.add_hline(y=overbought, line_dash="dash", line_color=COLORS["negative"], row=1, col=1)
    figure.add_hline(y=30, line_dash="dash", line_color=COLORS["positive"], row=1, col=1)
    histogram_colors = [
        COLORS["positive"] if value >= 0 else COLORS["negative"] for value in frame["macd_hist"].fillna(0)
    ]
    figure.add_trace(
        go.Bar(x=frame.index, y=frame["macd_hist"], name="Histograma", marker_color=histogram_colors),
        row=2,
        col=1,
    )
    figure.add_trace(go.Scatter(x=frame.index, y=frame["macd"], name="MACD"), row=2, col=1)
    figure.add_trace(
        go.Scatter(x=frame.index, y=frame["macd_signal"], name="Señal MACD"), row=2, col=1
    )
    figure.update_yaxes(range=[0, 100], row=1, col=1)
    figure.update_layout(height=520, hovermode="x unified", template="plotly_dark")
    return figure


def backtest_chart(curve: pd.DataFrame) -> go.Figure:
    figure = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.06, row_heights=[0.72, 0.28]
    )
    figure.add_trace(go.Scatter(x=curve.index, y=curve["equity"], name="Estrategia"), row=1, col=1)
    figure.add_trace(
        go.Scatter(x=curve.index, y=curve["buy_hold"], name="Buy & hold", line={"dash": "dash"}),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=curve.index,
            y=curve["drawdown"] * 100,
            name="Drawdown %",
            fill="tozeroy",
            line={"color": COLORS["negative"]},
        ),
        row=2,
        col=1,
    )
    figure.update_layout(height=590, hovermode="x unified", template="plotly_dark")
    figure.update_yaxes(title_text="Capital", row=1, col=1)
    figure.update_yaxes(title_text="Drawdown %", row=2, col=1)
    return figure
