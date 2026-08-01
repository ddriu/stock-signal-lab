"""Gráficos Plotly reutilizables por la interfaz."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


COLORS = {
    "price": "#0F172A",
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
        height=560,
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        template="plotly_white",
        legend={"orientation": "h", "y": 1.04},
        margin={"l": 35, "r": 20, "t": 70, "b": 35},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
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
    figure.update_layout(
        height=440,
        hovermode="x unified",
        template="plotly_white",
        legend={"orientation": "h", "y": 1.05},
        margin={"l": 35, "r": 20, "t": 50, "b": 35},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
    )
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
    figure.update_layout(
        height=500,
        hovermode="x unified",
        template="plotly_white",
        legend={"orientation": "h", "y": 1.05},
        margin={"l": 45, "r": 20, "t": 50, "b": 35},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
    )
    figure.update_yaxes(title_text="Capital", row=1, col=1)
    figure.update_yaxes(title_text="Drawdown %", row=2, col=1)
    return figure


def portfolio_evolution_chart(history: pd.DataFrame) -> go.Figure:
    """Compara valor de mercado, dinero neto aportado y resultado acumulado."""

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=history.index,
            y=history["market_value_eur"],
            name="Valor de la cartera",
            mode="lines",
            line={"color": COLORS["positive"], "width": 2.6},
            fill="tozeroy",
            fillcolor="rgba(32, 201, 151, 0.10)",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=history.index,
            y=history["net_contributions_eur"],
            name="Dinero neto aportado",
            mode="lines",
            line={"color": COLORS["medium"], "width": 2, "dash": "dash"},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=history.index,
            y=history["accumulated_result_eur"],
            name="Resultado acumulado",
            mode="lines",
            line={"color": COLORS["short"], "width": 1.8},
        )
    )
    figure.add_hline(y=0, line_dash="dot", line_color=COLORS["muted"])
    figure.update_layout(
        title="Cómo ha evolucionado la cartera",
        height=470,
        hovermode="x unified",
        template="plotly_white",
        legend={"orientation": "h", "y": 1.08},
        margin={"l": 45, "r": 20, "t": 75, "b": 35},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
        yaxis_title="Euros estimados",
    )
    return figure


def annual_portfolio_chart(annual: pd.DataFrame) -> go.Figure:
    """Resume la actividad anual y el valor alcanzado al cierre."""

    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(
        go.Bar(
            x=annual["Año"],
            y=annual["Compras EUR"],
            name="Compras",
            marker_color=COLORS["medium"],
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Bar(
            x=annual["Año"],
            y=annual["Ventas EUR"],
            name="Ventas",
            marker_color=COLORS["positive"],
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=annual["Año"],
            y=annual["Valor al cierre EUR"],
            name="Valor al cierre",
            mode="lines+markers",
            line={"color": COLORS["price"], "width": 2.6},
        ),
        secondary_y=True,
    )
    figure.update_layout(
        title="Compras, ventas y valor al cierre de cada año",
        barmode="group",
        height=430,
        hovermode="x unified",
        template="plotly_white",
        legend={"orientation": "h", "y": 1.09},
        margin={"l": 45, "r": 45, "t": 75, "b": 35},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
    )
    figure.update_yaxes(title_text="Movimientos (€)", secondary_y=False)
    figure.update_yaxes(title_text="Valor (€)", secondary_y=True)
    return figure


def private_investments_chart(investments: pd.DataFrame) -> go.Figure:
    """Compara capital todavía abierto y valor manual actual por plataforma."""

    open_investments = investments.loc[investments["status"] != "Finalizada"]
    summary = (
        open_investments.groupby("platform")[["invested_amount", "current_value"]]
        .sum()
        .reset_index()
    )
    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            x=summary["platform"],
            y=summary["invested_amount"],
            name="Capital abierto",
            marker_color=COLORS["medium"],
        )
    )
    figure.add_trace(
        go.Bar(
            x=summary["platform"],
            y=summary["current_value"],
            name="Valor actual manual",
            marker_color=COLORS["positive"],
        )
    )
    figure.update_layout(
        title="Capital todavía abierto en Civislend y Segofactoring",
        barmode="group",
        height=370,
        hovermode="x unified",
        template="plotly_white",
        legend={"orientation": "h", "y": 1.1},
        margin={"l": 45, "r": 20, "t": 75, "b": 35},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
        yaxis_title="Euros",
    )
    return figure


def portfolio_snapshot_allocation_chart(positions: pd.DataFrame) -> go.Figure:
    """Distribución por plataforma de la última fotografía disponible."""

    summary = (
        positions.groupby("platform", as_index=False)["value_eur"]
        .sum()
        .sort_values("value_eur", ascending=False)
    )
    figure = go.Figure(
        go.Pie(
            labels=summary["platform"],
            values=summary["value_eur"],
            hole=0.55,
            textinfo="label+percent",
            hovertemplate="%{label}<br>%{value:,.2f} €<br>%{percent}<extra></extra>",
        )
    )
    figure.update_layout(
        title="Distribución de la fotografía por plataforma",
        height=390,
        template="plotly_white",
        legend={"orientation": "h", "y": -0.08},
        margin={"l": 20, "r": 20, "t": 65, "b": 55},
        paper_bgcolor="rgba(0,0,0,0)",
    )
    return figure


def portfolio_snapshot_history_chart(positions: pd.DataFrame) -> go.Figure:
    """Evolución del valor declarado por plataforma entre fotografías."""

    summary = (
        positions.groupby(["snapshot_date", "platform"], as_index=False)["value_eur"]
        .sum()
        .sort_values("snapshot_date")
    )
    figure = go.Figure()
    for platform, group in summary.groupby("platform", sort=True):
        figure.add_trace(
            go.Scatter(
                x=pd.to_datetime(group["snapshot_date"]),
                y=group["value_eur"],
                name=str(platform),
                mode="lines+markers",
                stackgroup="total",
                hovertemplate="%{x|%d/%m/%Y}<br>%{y:,.2f} €<extra>%{fullData.name}</extra>",
            )
        )
    figure.update_layout(
        title="Evolución por fotografías guardadas",
        height=390,
        hovermode="x unified",
        template="plotly_white",
        legend={"orientation": "h", "y": 1.1},
        margin={"l": 45, "r": 20, "t": 75, "b": 35},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
        yaxis_title="Valor declarado (€)",
    )
    return figure


def normalized_comparison_chart(
    normalized_prices: pd.DataFrame,
    *,
    title: str,
) -> go.Figure:
    """Compara recorridos desde una base 100, no precios nominales."""

    figure = go.Figure()
    for ticker in normalized_prices.columns:
        series = normalized_prices[ticker].dropna()
        figure.add_trace(
            go.Scatter(
                x=series.index,
                y=series,
                name=str(ticker),
                mode="lines",
                line={"width": 2.2},
            )
        )
    figure.add_hline(y=100, line_dash="dot", line_color=COLORS["muted"])
    figure.update_layout(
        title=title,
        height=480,
        hovermode="x unified",
        template="plotly_white",
        legend={"orientation": "h", "y": 1.08},
        margin={"l": 45, "r": 20, "t": 75, "b": 35},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
        yaxis_title="Evolución con inicio = 100",
    )
    return figure


def risk_return_chart(metrics: pd.DataFrame, *, horizon_label: str) -> go.Figure:
    """Sitúa cada empresa por rendimiento y volatilidad del mismo periodo."""

    data = metrics.dropna(
        subset=["horizon_return_pct", "annualized_volatility_pct"]
    )
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=data["annualized_volatility_pct"],
            y=data["horizon_return_pct"],
            text=data.index.astype(str),
            mode="markers+text",
            textposition="top center",
            marker={
                "size": data["leadership_score"].fillna(50).clip(20, 100) / 3 + 10,
                "color": data["leadership_score"],
                "colorscale": "RdYlGn",
                "cmin": 0,
                "cmax": 100,
                "showscale": True,
                "colorbar": {"title": "Liderazgo"},
                "line": {"color": "#ffffff", "width": 1},
            },
            hovertemplate=(
                "<b>%{text}</b><br>Volatilidad anual: %{x:.1f}%"
                "<br>Rentabilidad: %{y:+.1f}%<extra></extra>"
            ),
        )
    )
    figure.add_hline(y=0, line_dash="dot", line_color=COLORS["muted"])
    figure.update_layout(
        title=f"Rentabilidad y movimiento del precio · {horizon_label}",
        height=440,
        template="plotly_white",
        margin={"l": 45, "r": 20, "t": 70, "b": 45},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
        xaxis_title="Volatilidad anualizada (%)",
        yaxis_title="Rentabilidad del periodo (%)",
    )
    return figure


def correlation_heatmap(correlations: pd.DataFrame) -> go.Figure:
    """Muestra qué acciones tienden a moverse de forma parecida."""

    figure = go.Figure(
        data=go.Heatmap(
            z=correlations.to_numpy(dtype=float),
            x=correlations.columns.astype(str),
            y=correlations.index.astype(str),
            zmin=-1,
            zmax=1,
            colorscale="RdBu",
            reversescale=True,
            text=correlations.round(2).to_numpy(),
            texttemplate="%{text}",
            hovertemplate="%{y} con %{x}: %{z:.2f}<extra></extra>",
            colorbar={"title": "Correlación"},
        )
    )
    figure.update_layout(
        title="Similitud de movimientos diarios",
        height=max(380, 48 * len(correlations)),
        template="plotly_white",
        margin={"l": 60, "r": 20, "t": 70, "b": 45},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
    )
    return figure
