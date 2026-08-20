"""Gráficos Plotly reutilizables por la interfaz."""

from __future__ import annotations

import textwrap

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

CHART_PERIODS = ("1 mes", "3 meses", "1 año", "5 años", "Máximo")


def chart_period_frame(frame: pd.DataFrame, period: str) -> pd.DataFrame:
    """Recorta un histórico ya calculado para una vista legible del gráfico."""

    if period not in CHART_PERIODS:
        raise ValueError(f"Periodo de gráfico no reconocido: {period}")
    if frame.empty or period == "Máximo":
        return frame.copy()
    offsets = {
        "1 mes": pd.DateOffset(months=1),
        "3 meses": pd.DateOffset(months=3),
        "1 año": pd.DateOffset(years=1),
        "5 años": pd.DateOffset(years=5),
    }
    cutoff = pd.Timestamp(frame.index[-1]) - offsets[period]
    return frame.loc[pd.to_datetime(frame.index) >= cutoff].copy()


def _wrapped_title(value: object, *, width: int = 38) -> str:
    """Divide títulos largos para que no desaparezcan en móvil o media columna."""

    text = str(value or "").strip()
    if not text:
        return ""
    return "<br>".join(
        textwrap.wrap(
            text,
            width=width,
            break_long_words=False,
            break_on_hyphens=False,
        )
    )


def _finalize_figure(figure: go.Figure) -> go.Figure:
    """Aplica legibilidad común sin fijar el ancho que decide Streamlit."""

    title_text = str(figure.layout.title.text or "").strip()
    wrapped_title = _wrapped_title(title_text)
    margin = figure.layout.margin.to_plotly_json()
    title_lines = wrapped_title.count("<br>") + 1 if wrapped_title else 0
    margin["t"] = max(int(margin.get("t") or 0), 70 + max(title_lines - 1, 0) * 24)
    margin["l"] = max(int(margin.get("l") or 0), 28)
    margin["r"] = max(int(margin.get("r") or 0), 28)
    margin["b"] = max(int(margin.get("b") or 0), 42)
    figure.update_layout(
        autosize=True,
        margin=margin,
        title={
            "text": wrapped_title,
            "x": 0.01,
            "xanchor": "left",
            "font": {"size": 18},
        },
        font={"size": 12},
        hoverlabel={"namelength": -1},
    )
    figure.update_xaxes(automargin=True)
    figure.update_yaxes(automargin=True)
    for trace in figure.data:
        if isinstance(trace, go.Pie):
            trace.automargin = True
        if isinstance(trace, (go.Bar, go.Scatter)):
            trace.cliponaxis = False
    return figure


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
    return _finalize_figure(figure)


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
    return _finalize_figure(figure)


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
    return _finalize_figure(figure)


def return_calibration_chart(summary: pd.DataFrame) -> go.Figure:
    """Compara frecuencias históricas positivas y de superación de objetivos."""

    figure = go.Figure()
    visible = summary.loc[
        ~summary["score_tier"].astype(str).str.startswith("Todas las")
    ].copy()
    for column, name, color in (
        ("positive_rate_pct", "Terminó en positivo", COLORS["medium"]),
        ("beat_sego_rate_pct", "Superó Segofactoring", COLORS["short"]),
        ("beat_civislend_rate_pct", "Superó Civislend", COLORS["positive"]),
    ):
        figure.add_trace(
            go.Bar(
                x=visible["score_tier"],
                y=visible[column],
                name=name,
                marker_color=color,
                text=visible[column].map(lambda value: f"{value:.1f}%"),
                textposition="outside",
            )
        )
    figure.update_layout(
        title="Qué ocurrió después de las señales históricas",
        barmode="group",
        height=430,
        template="plotly_white",
        yaxis={"title": "Frecuencia histórica", "range": [0, 105], "ticksuffix": "%"},
        legend={"orientation": "h", "y": 1.14},
        margin={"l": 45, "r": 20, "t": 95, "b": 55},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
    )
    return _finalize_figure(figure)


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
    return _finalize_figure(figure)


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
    return _finalize_figure(figure)


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
    return _finalize_figure(figure)


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
    return _finalize_figure(figure)


def portfolio_snapshot_assets_chart(
    positions: pd.DataFrame,
    *,
    max_assets: int = 12,
) -> go.Figure:
    """Muestra las partidas con mayor peso en la última fotografía."""

    summary = (
        positions.groupby(["asset_name", "platform"], as_index=False)["value_eur"]
        .sum()
        .sort_values("value_eur", ascending=False)
        .head(max(1, int(max_assets)))
        .sort_values("value_eur")
    )
    labels = summary["asset_name"].astype(str) + " · " + summary["platform"].astype(str)
    wrapped_labels = labels.map(lambda value: _wrapped_title(value, width=28))
    label_lines = sum(value.count("<br>") + 1 for value in wrapped_labels)
    figure = go.Figure(
        go.Bar(
            x=summary["value_eur"],
            y=wrapped_labels,
            customdata=labels,
            orientation="h",
            marker_color=COLORS["medium"],
            text=summary["value_eur"].map(lambda value: f"{value:,.0f} €"),
            textposition="outside",
            hovertemplate="%{customdata}<br>%{x:,.2f} €<extra></extra>",
        )
    )
    figure.update_layout(
        title=f"{len(summary)} partidas con mayor valor",
        height=max(390, 24 * label_lines + 115),
        template="plotly_white",
        margin={"l": 20, "r": 55, "t": 65, "b": 35},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
        xaxis_title="Valor declarado (€)",
        yaxis_title="",
    )
    return _finalize_figure(figure)


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
    return _finalize_figure(figure)


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
    return _finalize_figure(figure)


def staircase_projection_chart(projections: pd.DataFrame) -> go.Figure:
    """Compara escenarios de valor futuro con el dinero realmente aportado."""

    figure = go.Figure()
    if projections.empty:
        return _finalize_figure(figure)
    first_scenario = str(projections["scenario"].iloc[0])
    contributed = projections.loc[projections["scenario"] == first_scenario]
    figure.add_trace(
        go.Scatter(
            x=contributed["date"],
            y=contributed["contributed"],
            name="Capital aportado",
            mode="lines",
            line={"color": COLORS["muted"], "width": 2, "dash": "dot"},
            hovertemplate="%{x|%b %Y}<br>%{y:,.0f} € aportados<extra></extra>",
        )
    )
    palette = [
        COLORS["short"],
        COLORS["medium"],
        COLORS["positive"],
        "#7C3AED",
    ]
    for color, (scenario, group) in zip(
        palette,
        projections.groupby("scenario", sort=False),
    ):
        figure.add_trace(
            go.Scatter(
                x=group["date"],
                y=group["total_value"],
                name=str(scenario),
                mode="lines",
                line={"color": color, "width": 2.4},
                hovertemplate=(
                    "%{x|%b %Y}<br>%{y:,.0f} € estimados<extra>%{fullData.name}</extra>"
                ),
            )
        )
    figure.update_layout(
        title="Aportaciones y escenarios de crecimiento",
        height=480,
        hovermode="x unified",
        template="plotly_white",
        legend={"orientation": "h", "y": 1.12},
        margin={"l": 45, "r": 20, "t": 90, "b": 35},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
        yaxis_title="Valor estimado (€)",
    )
    return _finalize_figure(figure)


def staircase_range_chart(simulation: pd.DataFrame) -> go.Figure:
    """Muestra la mediana y el intervalo central de la simulación Monte Carlo."""

    figure = go.Figure()
    if simulation.empty:
        return _finalize_figure(figure)
    figure.add_trace(
        go.Scatter(
            x=simulation["date"],
            y=simulation["p90"],
            name="Escenario favorable (P90)",
            mode="lines",
            line={"color": "rgba(58,134,255,0.28)", "width": 1},
            hovertemplate="%{x|%b %Y}<br>%{y:,.0f} €<extra>P90</extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=simulation["date"],
            y=simulation["p10"],
            name="Escenario desfavorable (P10)",
            mode="lines",
            fill="tonexty",
            fillcolor="rgba(58,134,255,0.14)",
            line={"color": "rgba(58,134,255,0.28)", "width": 1},
            hovertemplate="%{x|%b %Y}<br>%{y:,.0f} €<extra>P10</extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=simulation["date"],
            y=simulation["p50"],
            name="Resultado central (mediana)",
            mode="lines",
            line={"color": COLORS["positive"], "width": 3},
            hovertemplate="%{x|%b %Y}<br>%{y:,.0f} €<extra>Mediana</extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=simulation["date"],
            y=simulation["contributed"],
            name="Capital aportado",
            mode="lines",
            line={"color": COLORS["muted"], "width": 2, "dash": "dot"},
            hovertemplate="%{x|%b %Y}<br>%{y:,.0f} €<extra>Aportado</extra>",
        )
    )
    figure.update_layout(
        title="Rango de resultados posibles · no es una promesa",
        height=480,
        hovermode="x unified",
        template="plotly_white",
        legend={"orientation": "h", "y": 1.12},
        margin={"l": 45, "r": 20, "t": 90, "b": 35},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
        yaxis_title="Valor estimado (€)",
    )
    return _finalize_figure(figure)


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
    return _finalize_figure(figure)


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
    return _finalize_figure(figure)
