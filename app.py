"""Interfaz Streamlit de Stock Signal Lab.

Ejecutar con: ``streamlit run app.py``
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from config import BacktestConfig, StrategyConfig
from src.auth import (
    AuthConfig,
    load_auth_accounts,
    managed_usernames,
    persistent_journal_enabled,
    require_login,
)
from src.backtesting import BacktestResult, run_backtest
from src.dashboard import build_position_dashboard
from src.data_loader import (
    DataDownloadError,
    TickerSearchResult,
    download_fundamental_snapshot,
    download_prices,
    search_instruments,
)
from src.data_sources import (
    ExternalDataError,
    FxSnapshot,
    PriceVerification,
    benchmark_for_ticker,
    compare_verified_price,
    convert_currency,
    download_alpha_vantage_latest_close,
    download_ecb_fx_snapshot,
    sector_benchmark,
)
from src.fundamentals import FundamentalResult, evaluate_fundamentals
from src.indicators import add_indicators
from src.journal import MAX_FAVORITES, calculate_open_positions
from src.supabase_journal import JournalStorageError
from src.storage import GROUP_PORTFOLIO_OWNER, create_journal
from src.opportunity import (
    OpportunityResult,
    RelativeStrengthResult,
    RiskResult,
    ValuationResult,
    combine_opportunity,
    evaluate_relative_strength,
    evaluate_risk,
    evaluate_valuation,
)
from src.portfolio import compare_switch, value_holding
from src.recommendations import (
    build_entry_guide,
    build_profit_taking_plan,
    historical_forward_return_study,
)
from src.risk import calculate_position_plan
from src.signal_engine import add_signal_columns, evaluate_latest_signal
from src.visualization import backtest_chart, momentum_chart, price_chart


st.set_page_config(page_title="Stock Signal Lab", page_icon="📈", layout="wide")


@st.cache_data(ttl=3_600, show_spinner=False)
def cached_download(ticker: str, start: date, end: date, auto_adjust: bool) -> pd.DataFrame:
    """Caché de red; los indicadores se recalculan fuera con la configuración actual."""

    return download_prices(ticker, start, end, auto_adjust=auto_adjust)


@st.cache_data(ttl=21_600, show_spinner=False)
def cached_fundamentals(ticker: str) -> dict[str, object]:
    """Caché más largo para datos empresariales, que cambian menos que el precio."""

    return download_fundamental_snapshot(ticker)


@st.cache_data(ttl=86_400, show_spinner=False)
def cached_fx_rates() -> FxSnapshot:
    """Tipos de referencia diarios; una consulta basta para todas las empresas."""

    return download_ecb_fx_snapshot()


@st.cache_data(ttl=86_400, show_spinner=False)
def cached_price_verification(ticker: str, api_key: str) -> PriceVerification:
    """Comprobación opcional del cierre mediante un segundo proveedor."""

    return download_alpha_vantage_latest_close(ticker, api_key)


@st.cache_data(ttl=86_400, show_spinner=False)
def cached_company_search(query: str) -> list[TickerSearchResult]:
    """Evita repetir búsquedas iguales durante el día."""

    return search_instruments(query)


def parse_tickers(raw_value: str) -> list[str]:
    values = raw_value.replace(";", ",").replace("\n", ",").split(",")
    return list(dict.fromkeys(value.strip().upper() for value in values if value.strip()))


def format_pct(value: float) -> str:
    return f"{value:+.2f}%"


PLAIN_SIGNAL_GUIDANCE = {
    "Entrada fuerte": (
        "El precio reúne una combinación especialmente fuerte de tendencia, impulso y liderazgo. "
        "Es el mejor momento técnico del radar, pero aún requiere revisar la empresa y el riesgo."
    ),
    "Entrada interesante": (
        "La empresa muestra una combinación fuerte de tendencia, impulso y liderazgo. "
        "Merece una revisión más profunda antes de decidir una entrada."
    ),
    "Vigilancia": (
        "Hay señales prometedoras, pero todavía falta alguna confirmación. Conviene seguirla "
        "sin tratarla aún como una entrada completa."
    ),
    "Esperar": (
        "La empresa puede ser interesante, pero el precio parece demasiado acelerado. "
        "Perseguirlo ahora aumenta el riesgo de comprar justo antes de una pausa."
    ),
    "Mantener": (
        "La tendencia sigue razonablemente sana, pero hoy no aparece una entrada especialmente clara."
    ),
    "Reducir": (
        "La fuerza se está debilitando. Si ya tienes la acción, conviene revisar cuánto riesgo "
        "quieres mantener; si no la tienes, todavía no destaca como nueva entrada."
    ),
    "Vender": (
        "La tendencia principal está dañada o se ha activado el límite de pérdida. "
        "No equivale a una certeza de caída, sino a una señal de deterioro."
    ),
}


def friendly_factor(text: str) -> str:
    """Convierte un fragmento técnico en una frase legible."""

    return text[:1].upper() + text[1:] if text else text


def build_favorite_catalog(
    private_favorites: pd.DataFrame,
    group_favorites: pd.DataFrame,
) -> tuple[list[str], dict[str, str]]:
    """Une favoritos privados y compartidos sin duplicar empresas."""

    sources: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    for frame, source in (
        (private_favorites, "privada"),
        (group_favorites, "grupo"),
    ):
        if frame.empty:
            continue
        for favorite in frame.itertuples(index=False):
            ticker = str(favorite.ticker).strip().upper()
            if not ticker:
                continue
            sources.setdefault(ticker, set()).add(source)
            name = str(getattr(favorite, "name", "") or ticker).strip()
            names.setdefault(ticker, name)
    tickers = sorted(names, key=lambda item: (names[item].casefold(), item))
    labels = {
        ticker: (
            f"{names[ticker]} ({ticker}) · "
            + " y ".join(sorted(sources[ticker]))
        )
        for ticker in tickers
    }
    return tickers, labels


def build_sidebar(
    favorite_tickers: list[str] | None = None,
    favorite_labels: dict[str, str] | None = None,
) -> tuple[
    list[str],
    date,
    date,
    bool,
    str,
    StrategyConfig,
    BacktestConfig,
    bool,
]:
    favorite_tickers = favorite_tickers or []
    favorite_labels = favorite_labels or {}
    st.sidebar.header("1. Empresas y periodo")
    selected_favorites = st.sidebar.multiselect(
        "Elegir de mis favoritos",
        options=favorite_tickers,
        format_func=lambda ticker: favorite_labels.get(ticker, ticker),
        max_selections=25,
        help=(
            "Puedes guardar hasta 100 y analizar hasta 25 en profundidad cada vez. "
            "Las posiciones abiertas se actualizan además en modo rápido."
        ),
    )
    with st.sidebar.expander("Añadir símbolos manualmente"):
        manual_tickers = parse_tickers(
            st.text_area(
                "Símbolos bursátiles",
                "" if favorite_tickers else "AAPL, MSFT, SAN.MC",
                height=80,
                help=(
                    "Alternativa al buscador. Escribe símbolos separados por comas; "
                    "SAN.MC indica la bolsa de Madrid."
                ),
            )
        )
    tickers = list(dict.fromkeys([*selected_favorites, *manual_tickers]))
    years = st.sidebar.select_slider(
        "Años de datos para analizar",
        options=[1, 2, 3, 5, 10],
        value=5,
        format_func=lambda x: f"{x} años",
        help="Cinco años es un buen punto de partida. Un año ofrece muy poco historial para probar reglas.",
    )
    end = st.sidebar.date_input(
        "Analizar hasta",
        value=date.today(),
        max_value=date.today(),
        help="Normalmente se deja en la fecha actual.",
    )
    start = end - timedelta(days=365 * years)
    auto_adjust = st.sidebar.checkbox("Precios ajustados", value=True, help="Ajusta splits y dividendos según yfinance.")
    with st.sidebar.expander("Fuentes de datos"):
        st.caption(
            "Yahoo aporta precios y contexto. Para tickers de EE. UU., la app intenta "
            "validar las cuentas con SEC EDGAR. El BCE aporta divisas."
        )
        alpha_vantage_key = st.text_input(
            "Clave gratuita de Alpha Vantage (opcional)",
            type="password",
            help=(
                "Si la introduces, se compara el último cierre con un segundo proveedor. "
                "La cuenta gratuita tiene un límite diario reducido."
            ),
        )

    st.sidebar.header("2. Cómo medir la tendencia")
    col_a, col_b, col_c = st.sidebar.columns(3)
    sma_short = col_a.number_input(
        "Corta", min_value=2, max_value=100, value=20, help="Ritmo reciente del precio."
    )
    sma_medium = col_b.number_input(
        "Media", min_value=5, max_value=250, value=50, help="Tendencia de varios meses."
    )
    sma_long = col_c.number_input(
        "Larga", min_value=20, max_value=500, value=200, help="Tendencia principal."
    )
    st.sidebar.caption("20 / 50 / 200 días es la configuración recomendada para empezar.")
    rsi_period = st.sidebar.number_input(
        "Días para medir el impulso",
        min_value=2,
        max_value=50,
        value=14,
        help="Cuantos menos días, más rápida pero más nerviosa será la señal.",
    )
    rsi_range = st.sidebar.slider(
        "Zona de impulso saludable",
        20,
        80,
        (45, 68),
        help="Premia empresas que avanzan con fuerza sin estar extremadamente aceleradas.",
    )
    rsi_overbought = st.sidebar.slider(
        "Nivel de precio demasiado acelerado",
        60,
        95,
        78,
        help="Por encima de este nivel la aplicación sugiere esperar una entrada mejor.",
    )
    max_distance = st.sidebar.slider(
        "Distancia máxima frente a su ritmo reciente",
        2.0,
        30.0,
        12.0,
        0.5,
        format="%.1f%%",
        help="Evita perseguir precios excesivamente alejados de la media corta.",
    )

    with st.sidebar.expander("Opciones avanzadas de búsqueda"):
        st.caption("Puedes mantener estos valores hasta tener suficientes backtests.")
        watch_score = st.slider(
            "Empezar a vigilar desde", 45, 70, 55,
            help="Desde 55 puntos la empresa entra en la lista de vigilancia técnica."
        )
        buy_score = st.slider(
            "Entrada interesante desde", 55, 80, 65,
            help="A partir de 65 puntos, con tendencia confirmada, aparece como entrada interesante."
        )
        strong_score = st.slider(
            "Entrada fuerte desde", 65, 95, 75,
            help="Reserva esta etiqueta para las configuraciones técnicas más completas."
        )
        reduce_score = st.slider(
            "Nivel de debilidad", 20, 60, 40,
            help="Por debajo de este nivel se revisan posiciones cuya tendencia ya se debilita."
        )
        sell_score = st.slider(
            "Nivel de deterioro severo", 0, 40, 25,
            help="Un score bajo no basta por sí solo: también debe existir pérdida de tendencia."
        )
        confirmation_days = st.slider(
            "Días para confirmar una señal negativa",
            1,
            5,
            2,
            format="%d sesiones",
            help="Evita reaccionar a una sola sesión mala.",
        )
        breakout_period = st.slider(
            "Días que debe superar para marcar nuevo máximo",
            10,
            60,
            20,
            format="%d sesiones",
            help="Detecta empresas que están rompiendo su rango reciente.",
        )
        near_high = st.slider(
            "Distancia admitida desde su máximo anual",
            5.0,
            30.0,
            12.0,
            1.0,
            format="%.0f%%",
            help="Las empresas líderes suelen cotizar relativamente cerca de sus máximos.",
        )
        volume_surge = st.slider(
            "Actividad de compra destacada",
            1.1,
            3.0,
            1.2,
            0.1,
            format="%.1fx",
            help="1,2x significa un 20% más de volumen que su media reciente.",
        )
        volume_normal = st.slider(
            "Actividad mínima normal",
            0.5,
            1.0,
            0.8,
            0.1,
            format="%.1fx",
            help="Desde 0,8x ya suma puntos; superar 1,2x añade una bonificación.",
        )

    st.sidebar.header("3. Dinero y riesgo")
    stop_loss = st.sidebar.slider(
        "Pérdida máxima desde la entrada",
        1.0,
        30.0,
        8.0,
        0.5,
        format="%.1f%%",
        help="Nivel aproximado en el que se cerraría una posición que sale mal.",
    )
    trailing_stop = st.sidebar.slider(
        "Protección de beneficios (0 = desactivada)",
        0.0,
        30.0,
        10.0,
        0.5,
        format="%.1f%%",
        help="El nivel de salida va subiendo cuando la acción alcanza nuevos máximos.",
    )
    max_risk = st.sidebar.slider(
        "Capital que aceptas perder en una operación",
        0.1,
        10.0,
        1.0,
        0.1,
        format="%.1f%%",
        help="No es el porcentaje invertido; es la pérdida aproximada si se alcanza el stop.",
    )
    forward_horizon = st.sidebar.selectbox(
        "Horizonte para estimar resultados",
        options=[10, 20, 40, 60],
        index=1,
        format_func=lambda value: f"{value} sesiones",
        help="20 sesiones equivalen aproximadamente a un mes bursátil.",
    )
    exit_on_reduce = st.sidebar.checkbox(
        "Cerrar la posición si aparece «Reducir»",
        value=True,
        help="En la prueba histórica, Reducir se interpreta como una salida completa.",
    )
    initial_capital = st.sidebar.number_input(
        "Capital disponible para la simulación",
        min_value=100.0,
        value=10_000.0,
        step=1_000.0,
        help="Se usa para el backtest y para calcular un tamaño orientativo de posición.",
    )
    commission = st.sidebar.number_input(
        "Coste de compra o venta (%)",
        min_value=0.0,
        value=0.10,
        step=0.05,
        format="%.2f",
        help="Comisión aproximada de tu intermediario.",
    )
    slippage = st.sidebar.number_input(
        "Margen por ejecución imperfecta (%)",
        min_value=0.0,
        value=0.05,
        step=0.05,
        format="%.2f",
        help="Simula que normalmente no se compra o vende exactamente al precio observado.",
    )

    strategy = StrategyConfig(
        sma_short=int(sma_short),
        sma_medium=int(sma_medium),
        sma_long=int(sma_long),
        rsi_period=int(rsi_period),
        rsi_buy_min=float(rsi_range[0]),
        rsi_buy_max=float(rsi_range[1]),
        rsi_overbought=float(rsi_overbought),
        distance_from_sma20_pct=float(max_distance),
        breakout_period=int(breakout_period),
        near_high_pct=float(near_high),
        volume_normal_ratio=float(volume_normal),
        volume_surge_ratio=float(volume_surge),
        watch_score_threshold=int(watch_score),
        buy_score_threshold=int(buy_score),
        strong_score_threshold=int(strong_score),
        forward_horizon_days=int(forward_horizon),
        reduce_score_threshold=int(reduce_score),
        sell_score_threshold=int(sell_score),
        trend_confirmation_days=int(confirmation_days),
        stop_loss_pct=float(stop_loss),
        trailing_stop_pct=float(trailing_stop),
        max_risk_per_trade_pct=float(max_risk),
        exit_on_reduce=exit_on_reduce,
    )
    backtest = BacktestConfig(
        initial_capital=float(initial_capital),
        commission_pct=float(commission),
        slippage_pct=float(slippage),
    )
    load_clicked = st.sidebar.button(
        "Descargar / actualizar",
        type="primary",
        width="stretch",
    )
    return (
        tickers,
        start,
        end,
        auto_adjust,
        alpha_vantage_key,
        strategy,
        backtest,
        load_clicked,
    )


def load_market_data(
    tickers: list[str],
    start: date,
    end: date,
    auto_adjust: bool,
    alpha_vantage_key: str = "",
    *,
    fundamental_tickers: set[str] | None = None,
) -> None:
    if not tickers:
        st.sidebar.error("Elige al menos una favorita o registra una posición.")
        return
    if len(tickers) > 200:
        st.sidebar.error(
            "La actualización admite hasta 200 empresas simultáneas. "
            "Reduce temporalmente la selección de favoritos."
        )
        return
    deep_tickers = set(tickers if fundamental_tickers is None else fundamental_tickers)
    if len(deep_tickers) > 25:
        deep_tickers = set(
            [ticker for ticker in tickers if ticker in deep_tickers][:25]
        )
        st.sidebar.warning(
            "Se hará análisis empresarial profundo de las primeras 25 empresas. "
            "Las demás conservarán valoración rápida de precio y tendencia."
        )
    downloaded: dict[str, pd.DataFrame] = {}
    fundamentals: dict[str, dict[str, object]] = {}
    verifications: dict[str, PriceVerification] = {}
    errors: list[str] = []
    progress = st.progress(0, text="Descargando precios…")
    for position, ticker in enumerate(tickers, start=1):
        ticker_start = (
            start
            if ticker in deep_tickers
            else max(start, end - timedelta(days=420))
        )
        try:
            downloaded[ticker] = cached_download(
                ticker,
                ticker_start,
                end,
                auto_adjust,
            )
        except (DataDownloadError, ValueError) as exc:
            errors.append(str(exc))
        if ticker in deep_tickers:
            try:
                fundamentals[ticker] = cached_fundamentals(ticker)
            except (DataDownloadError, ValueError) as exc:
                fundamentals[ticker] = {"symbol": ticker}
                errors.append(str(exc))
            if alpha_vantage_key and ticker in downloaded:
                try:
                    verification = cached_price_verification(ticker, alpha_vantage_key)
                    verifications[ticker] = compare_verified_price(
                        downloaded[ticker],
                        verification,
                    )
                except (ExternalDataError, ValueError) as exc:
                    errors.append(str(exc))
        else:
            fundamentals[ticker] = {"symbol": ticker, "_quick_mode": True}
        mode = "análisis completo" if ticker in deep_tickers else "actualización rápida"
        progress.progress(
            position / len(tickers),
            text=f"{ticker}: {mode}",
        )
    reference_symbols: set[str] = set()
    for ticker in downloaded:
        reference_symbols.add(benchmark_for_ticker(ticker))
        sector_reference = sector_benchmark(
            str(fundamentals.get(ticker, {}).get("sector") or ""),
            ticker,
        )
        if sector_reference:
            reference_symbols.add(sector_reference)
    references: dict[str, pd.DataFrame] = {}
    for symbol in sorted(reference_symbols.difference(downloaded)):
        try:
            references[symbol] = cached_download(symbol, start, end, auto_adjust)
        except (DataDownloadError, ValueError) as exc:
            errors.append(f"No se pudo calcular la comparación con {symbol}: {exc}")
    try:
        fx_snapshot = cached_fx_rates()
    except (ExternalDataError, ValueError) as exc:
        fx_snapshot = FxSnapshot(as_of=None, rates_per_eur={"EUR": 1.0})
        errors.append(str(exc))
    progress.empty()
    st.session_state["market_data"] = downloaded
    st.session_state["fundamental_data"] = fundamentals
    st.session_state["reference_data"] = references
    st.session_state["price_verifications"] = verifications
    st.session_state["fx_snapshot"] = fx_snapshot
    st.session_state["quick_mode_tickers"] = sorted(set(tickers).difference(deep_tickers))
    st.session_state["download_errors"] = errors
    st.session_state.pop("backtest_result", None)


def prepare_data(
    raw_data: dict[str, pd.DataFrame],
    raw_fundamentals: dict[str, dict[str, object]],
    reference_data: dict[str, pd.DataFrame],
    config: StrategyConfig,
) -> tuple[
    dict[str, pd.DataFrame],
    list[dict[str, object]],
    dict[str, FundamentalResult],
    dict[str, ValuationResult],
    dict[str, RelativeStrengthResult],
    dict[str, RiskResult],
    dict[str, OpportunityResult],
]:
    prepared: dict[str, pd.DataFrame] = {}
    summary: list[dict[str, object]] = []
    fundamental_results: dict[str, FundamentalResult] = {}
    valuation_results: dict[str, ValuationResult] = {}
    relative_results: dict[str, RelativeStrengthResult] = {}
    risk_results: dict[str, RiskResult] = {}
    opportunity_results: dict[str, OpportunityResult] = {}
    for ticker, raw_frame in raw_data.items():
        try:
            frame = add_signal_columns(add_indicators(raw_frame, config), config)
            signal = evaluate_latest_signal(frame, config, ticker=ticker)
        except ValueError as exc:
            summary.append({"Ticker": ticker, "Estado": f"Sin señal: {exc}"})
            continue
        prepared[ticker] = frame
        fundamentals = evaluate_fundamentals(raw_fundamentals.get(ticker, {}), ticker)
        fundamental_results[ticker] = fundamentals
        valuation = evaluate_valuation(raw_fundamentals.get(ticker, {}), ticker)
        valuation_results[ticker] = valuation
        broad_name = benchmark_for_ticker(ticker)
        sector_name = sector_benchmark(fundamentals.sector, ticker)
        relative = evaluate_relative_strength(
            ticker,
            frame,
            reference_data.get(broad_name),
            broad_name=broad_name,
            sector=reference_data.get(sector_name) if sector_name else None,
            sector_name=sector_name,
        )
        relative_results[ticker] = relative
        risk = evaluate_risk(ticker, frame)
        risk_results[ticker] = risk
        opportunity = combine_opportunity(
            ticker,
            fundamentals,
            valuation,
            signal,
            relative,
            risk,
        )
        opportunity_results[ticker] = opportunity
        latest = frame.dropna(subset=["sma_long", "rsi"]).iloc[-1]
        summary.append(
            {
                "Ticker": ticker,
                "Oportunidad": opportunity.score,
                "Confianza datos": opportunity.confidence_pct,
                "Lectura conjunta": opportunity.label,
                "Calidad empresa": (
                    float(fundamentals.score) if fundamentals.score is not None else float("nan")
                ),
                "Valoración": (
                    float(valuation.score) if valuation.score is not None else float("nan")
                ),
                "Momento entrada": signal.score,
                "Fuerza relativa": (
                    float(relative.score) if relative.score is not None else float("nan")
                ),
                "Riesgo controlado": (
                    float(risk.score) if risk.score is not None else float("nan")
                ),
                "Lectura entrada": signal.label,
                "Si ya la tienes": signal.position_label,
                "Cierre": float(latest["close"]),
                "RSI": float(latest["rsi"]),
                "Fuerza 3 meses": float(latest["momentum_medium_pct"]),
                "Desde su máximo": float(latest["distance_high_pct"]),
                "Actividad": float(latest["volume_ratio"]),
                "Nuevo máximo reciente": "Sí" if bool(latest["breakout"]) else "No",
                "Fecha": signal.as_of.date(),
            }
        )
    return (
        prepared,
        summary,
        fundamental_results,
        valuation_results,
        relative_results,
        risk_results,
        opportunity_results,
    )


def render_analysis(
    ticker: str,
    frame: pd.DataFrame,
    strategy: StrategyConfig,
    settings: BacktestConfig,
    fundamentals: FundamentalResult,
    valuation: ValuationResult,
    relative: RelativeStrengthResult,
    risk: RiskResult,
    opportunity: OpportunityResult,
    raw_fundamentals: dict[str, object],
    verification: PriceVerification | None = None,
) -> None:
    with st.expander("Si ya tienes esta acción"):
        entry_price = st.number_input(
            "Precio de entrada (0 = sin posición)",
            min_value=0.0,
            value=0.0,
            step=0.01,
            key=f"entry_price_{ticker}",
            help="Permite comprobar si el cierre ha activado el stop loss configurado.",
        )
    signal = evaluate_latest_signal(
        frame,
        strategy,
        ticker=ticker,
        entry_price=float(entry_price) if entry_price > 0 else None,
    )
    latest = frame.dropna(subset=["sma_long", "rsi"]).iloc[-1]
    previous = frame["close"].iloc[-2]
    daily_change = (latest["close"] / previous - 1) * 100
    col1, col2, col3, col4 = st.columns(4)
    col1.metric(
        "Oportunidad conjunta",
        f"{opportunity.score}/100",
        opportunity.label,
        help="Combina cinco familias de análisis sin ocultar sus notas individuales.",
    )
    col2.metric(
        "Confianza de los datos",
        f"{opportunity.confidence_pct}%",
        help="Cobertura disponible; no es una probabilidad de ganar.",
    )
    col3.metric(
        "Calidad empresarial",
        f"{fundamentals.score}/100" if fundamentals.score is not None else "N/D",
        help="Rentabilidad, crecimiento, deuda y caja. N/D significa que faltan datos suficientes.",
    )
    col4.metric(
        "Valoración",
        f"{valuation.score}/100" if valuation.score is not None else "N/D",
        help="Precio frente a beneficios, crecimiento, caja y patrimonio. Debe compararse dentro del sector.",
    )
    col5, col6, col7, col8 = st.columns(4)
    col5.metric(
        "Momento de entrada",
        f"{signal.score}/100",
        help="Resume tendencia, impulso, liderazgo, volumen y calidad de entrada.",
    )
    col6.metric(
        "Fortaleza frente al mercado",
        f"{relative.score}/100" if relative.score is not None else "N/D",
        help="Compara su avance con el índice general y, cuando existe, con su sector.",
    )
    col7.metric(
        "Riesgo controlado",
        f"{risk.score}/100" if risk.score is not None else "N/D",
        help="Una nota alta indica menor volatilidad, menor caída y mejor liquidez; no elimina el riesgo.",
    )
    col8.metric("Último cierre", f"{latest['close']:.2f}", format_pct(daily_change))
    if opportunity.label in {"Oportunidad destacada", "Candidata"}:
        st.success(f"**{opportunity.label}:** {opportunity.explanation}")
    elif opportunity.label == "Vigilancia":
        st.info(f"**{opportunity.label}:** {opportunity.explanation}")
    else:
        st.warning(f"**{opportunity.label}:** {opportunity.explanation}")
    st.caption(
        f"Lectura técnica: {signal.label} · Si ya la tienes: {signal.position_label} · "
        f"RSI: {latest['rsi']:.1f}"
    )
    plain_message = PLAIN_SIGNAL_GUIDANCE.get(signal.label, signal.explanation)
    if signal.label in {"Entrada fuerte", "Entrada interesante"}:
        st.success(f"**{signal.label}:** {plain_message}")
    elif signal.label == "Vigilancia":
        st.info(f"**{signal.label}:** {plain_message}")
    else:
        st.warning(f"**{signal.label}:** {plain_message}")

    company_tab, entry_tab, risk_data_tab = st.tabs(
        ["Empresa y valoración", "Momento y liderazgo", "Riesgo y fuentes"]
    )
    with company_tab:
        if fundamentals.score is None:
            st.warning(
                f"Sólo está disponible el {fundamentals.coverage_pct}% de las métricas necesarias. "
                "La aplicación muestra N/D para no convertir datos ausentes en una nota engañosa."
            )
        else:
            context = " · ".join(
                value for value in (fundamentals.sector, fundamentals.country) if value
            )
            st.caption(
                f"Cobertura de datos: {fundamentals.coverage_pct}%"
                + (f" · {context}" if context else "")
            )
        good_company, company_risks = st.columns(2)
        with good_company:
            st.markdown("**Fortalezas empresariales**")
            if fundamentals.positive_factors:
                for factor in fundamentals.positive_factors:
                    st.markdown(f"- {friendly_factor(factor)}")
            else:
                st.caption("No hay suficientes fortalezas fundamentales disponibles.")
        with company_risks:
            st.markdown("**Debilidades empresariales**")
            if fundamentals.risk_factors:
                for factor in fundamentals.risk_factors:
                    st.markdown(f"- {friendly_factor(factor)}")
            else:
                st.caption("No aparecen debilidades fundamentales con los datos disponibles.")
        st.caption(
            "La nota de calidad no incluye valoración, riesgo país, divisa, regulación, "
            "gobierno corporativo ni ventajas competitivas."
        )
        st.markdown("**¿El precio parece razonable?**")
        if valuation.score is None:
            st.warning(
                f"Cobertura de valoración: {valuation.coverage_pct}%. "
                "Faltan múltiplos suficientes para asignar una nota."
            )
        else:
            st.caption(f"Cobertura de valoración: {valuation.coverage_pct}%")
        valuation_cols = st.columns(2)
        with valuation_cols[0]:
            for factor in valuation.positive_factors:
                st.markdown(f"- {friendly_factor(factor)}")
        with valuation_cols[1]:
            for factor in valuation.risk_factors:
                st.markdown(f"- {friendly_factor(factor)}")
        if valuation.metrics:
            metric_labels = {
                "PER futuro": "PER futuro",
                "PER histórico": "PER histórico",
                "PEG": "PEG",
                "Flujo de caja libre / capitalización (%)": "Rentabilidad de caja",
                "Precio / valor contable": "Precio/valor contable",
            }
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Métrica": metric_labels.get(name, name), "Valor": value}
                        for name, value in valuation.metrics
                    ]
                ),
                width="stretch",
                hide_index=True,
            )

    with entry_tab:
        favorable, caution = st.columns(2)
        with favorable:
            st.markdown("**Lo que favorece una entrada**")
            if signal.positive_factors:
                for factor in signal.positive_factors:
                    st.markdown(f"- {friendly_factor(factor)}")
            else:
                st.caption("No hay factores técnicos positivos suficientes.")
        with caution:
            st.markdown("**Lo que conviene vigilar**")
            if signal.risk_factors:
                for factor in signal.risk_factors:
                    st.markdown(f"- {friendly_factor(factor)}")
            else:
                st.caption("No aparecen alertas técnicas importantes.")
        st.markdown("**Comparación con el mercado**")
        if relative.score is None:
            st.warning("No hay suficiente historial del índice para medir liderazgo relativo.")
        else:
            st.caption(
                f"Índice general: {relative.broad_benchmark or 'N/D'}"
                + (
                    f" · Referencia sectorial: {relative.sector_benchmark}"
                    if relative.sector_benchmark
                    else ""
                )
            )
            strength_cols = st.columns(3)
            strength_cols[0].metric(
                "Rentabilidad 3 meses",
                (
                    format_pct(relative.stock_return_3m_pct)
                    if relative.stock_return_3m_pct is not None
                    else "N/D"
                ),
            )
            strength_cols[1].metric(
                "Ventaja frente al mercado",
                (
                    f"{relative.broad_excess_3m_pct:+.1f} puntos"
                    if relative.broad_excess_3m_pct is not None
                    else "N/D"
                ),
            )
            strength_cols[2].metric(
                "Ventaja frente al sector",
                (
                    f"{relative.sector_excess_3m_pct:+.1f} puntos"
                    if relative.sector_excess_3m_pct is not None
                    else "N/D"
                ),
            )
            for factor in (*relative.positive_factors, *relative.risk_factors):
                st.markdown(f"- {friendly_factor(factor)}")

    with risk_data_tab:
        risk_cols = st.columns(4)
        risk_cols[0].metric(
            "Volatilidad anual",
            (
                f"{risk.annualized_volatility_pct:.1f}%"
                if risk.annualized_volatility_pct is not None
                else "N/D"
            ),
        )
        risk_cols[1].metric(
            "Peor caída en un año",
            (
                f"{risk.max_drawdown_1y_pct:.1f}%"
                if risk.max_drawdown_1y_pct is not None
                else "N/D"
            ),
        )
        risk_cols[2].metric(
            "Movimiento diario típico",
            f"{risk.atr_pct:.1f}%" if risk.atr_pct is not None else "N/D",
        )
        risk_cols[3].metric(
            "Negociación media diaria",
            (
                f"{risk.average_turnover_20d:,.0f}"
                if risk.average_turnover_20d is not None
                else "N/D"
            ),
        )
        for factor in (*risk.positive_factors, *risk.risk_factors):
            st.markdown(f"- {friendly_factor(factor)}")
        source_names = fundamentals.source_names or ("Yahoo Finance",)
        st.markdown("**Procedencia de los datos**")
        st.write(" · ".join(source_names))
        if fundamentals.official_period_end:
            st.caption(
                f"Último periodo oficial utilizado: {fundamentals.official_period_end}"
            )
        if fundamentals.official_url:
            st.markdown(f"[Abrir presentación oficial en SEC EDGAR]({fundamentals.official_url})")
        if verification:
            difference_text = (
                f"{verification.difference_pct:+.2f}%"
                if verification.difference_pct is not None
                else "no comparable"
            )
            if verification.status == "Revisar diferencia":
                st.error(
                    f"El cierre de {verification.provider} difiere {difference_text} "
                    f"del dato principal en {verification.as_of}."
                )
            else:
                st.info(
                    f"Comprobación {verification.provider}: {verification.status} "
                    f"({difference_text}, {verification.as_of})."
                )
        else:
            st.caption(
                "No se ha configurado una segunda fuente de precios. Puedes añadir una "
                "clave gratuita de Alpha Vantage en «Fuentes de datos»."
            )
        for warning in raw_fundamentals.get("_warnings", []):
            st.warning(str(warning))

    if entry_price > 0 and signal.position_label in {"Reducir", "Vender"}:
        st.warning(
            f"Para la posición introducida, la lectura de gestión es «{signal.position_label}»."
        )

    plan = calculate_position_plan(
        capital=settings.initial_capital,
        entry_price=float(latest["close"]),
        stop_loss_pct=strategy.stop_loss_pct,
        max_risk_pct=strategy.max_risk_per_trade_pct,
    )
    with st.expander(
        "¿Cuánto arriesgaría si decidiera entrar?",
        expanded=signal.label in {"Entrada fuerte", "Entrada interesante"},
    ):
        st.caption(
            "Ejemplo matemático usando el último cierre como entrada. Asume que el capital y "
            "la acción están en la misma moneda; no incluye impuestos ni gaps."
        )
        risk_cols = st.columns(5)
        risk_cols[0].metric("Importe de la posición", f"{plan.position_value:,.2f}")
        risk_cols[1].metric("Unidades aproximadas", f"{plan.quantity:,.2f}")
        risk_cols[2].metric("Salida si falla", f"{plan.stop_price:,.2f}")
        risk_cols[3].metric("Pérdida prevista", f"{plan.loss_at_stop:,.2f}")
        risk_cols[4].metric("Referencia 2 a 1", f"{plan.reference_target_2r:,.2f}")
        st.caption(
            "«Referencia 2 a 1» significa que la ganancia potencial sería dos veces el riesgo. "
            "No es un precio objetivo ni una predicción."
        )

    study = historical_forward_return_study(
        frame,
        current_score=signal.score,
        horizon_days=strategy.forward_horizon_days,
    )
    entry_guide = build_entry_guide(
        fundamental_score=fundamentals.score,
        technical_score=signal.score,
        entry_label=signal.label,
        maximum_position_value=plan.position_value,
        current_price=float(latest["close"]),
        study=study,
    )
    st.subheader("Guía probabilística de compra")
    guide_cols = st.columns(4)
    guide_cols[0].metric("Guía", entry_guide.label)
    guide_cols[1].metric("Importe inicial orientativo", f"{entry_guide.initial_amount:,.2f}")
    guide_cols[2].metric("Unidades iniciales", f"{entry_guide.initial_quantity:,.3f}")
    guide_cols[3].metric(
        "Máximo según tu riesgo", f"{entry_guide.maximum_position_value:,.2f}"
    )
    st.write(entry_guide.rationale)
    if study.reliable:
        expected_price = float(latest["close"]) * (
            1 + float(study.median_return_pct) / 100
        )
        history_cols = st.columns(5)
        history_cols[0].metric("Casos históricos comparables", study.samples)
        history_cols[1].metric(
            f"Retorno mediano a {study.horizon_days} sesiones",
            format_pct(float(study.median_return_pct)),
        )
        history_cols[2].metric(
            "Casos positivos", f"{float(study.positive_rate_pct):.1f}%"
        )
        history_cols[3].metric(
            "Rango histórico central",
            f"{float(study.lower_quartile_pct):+.1f}% a "
            f"{float(study.upper_quartile_pct):+.1f}%",
        )
        history_cols[4].metric("Precio estadístico orientativo", f"{expected_price:,.2f}")
        st.caption(
            "La estimación es la mediana de señales históricas comparables y no una predicción. "
            "No incorpora resultados fundamentales futuros, noticias, impuestos ni gaps."
        )
    else:
        st.warning(
            f"Sólo se encontraron {study.samples} casos históricos comparables. "
            "Se necesitan al menos 8 para mostrar una estimación de retorno."
        )

    with st.expander("Ver explicación técnica completa"):
        st.write(signal.explanation)
    st.plotly_chart(price_chart(frame, ticker), width="stretch")
    st.plotly_chart(momentum_chart(frame, strategy.rsi_overbought), width="stretch")


def render_backtest(ticker: str, frame: pd.DataFrame, strategy: StrategyConfig, settings: BacktestConfig) -> None:
    st.caption(
        "Esta prueba simula qué habría pasado aplicando las mismas reglas en el pasado. "
        "Incluye costes y límites de pérdida, pero el pasado no garantiza resultados futuros."
    )
    if st.button("Probar las reglas con datos pasados", type="primary"):
        try:
            with st.spinner("Simulando estrategia…"):
                result = run_backtest(frame, strategy, settings)
            st.session_state["backtest_result"] = result
            st.session_state["backtest_ticker"] = ticker
        except ValueError as exc:
            st.error(str(exc))
            return
    result: BacktestResult | None = st.session_state.get("backtest_result")
    if result is None or st.session_state.get("backtest_ticker") != ticker:
        st.info("Pulsa el botón para probar este ticker con datos pasados.")
        return
    metrics = result.metrics
    cols = st.columns(5)
    cols[0].metric("Resultado de las reglas", format_pct(float(metrics["total_return_pct"])))
    cols[1].metric("Comprar y mantener", format_pct(float(metrics["buy_hold_return_pct"])))
    cols[2].metric(
        "Peor caída temporal",
        format_pct(float(metrics["max_drawdown_pct"])),
        help="Mayor descenso sufrido desde un máximo anterior de la cartera.",
    )
    cols[3].metric(
        "Operaciones ganadoras",
        f"{float(metrics['win_rate_pct']):.1f}%",
        help="Porcentaje de operaciones cerradas con beneficio; no indica cuánto se ganó o perdió.",
    )
    cols[4].metric("Operaciones cerradas", int(metrics["completed_trades"]))
    st.plotly_chart(backtest_chart(result.equity_curve), width="stretch")
    with st.expander("Métricas adicionales"):
        st.json(
            {
                "Capital final": round(float(metrics["final_equity"]), 2),
                "Volatilidad anualizada (%)": round(float(metrics["annualized_volatility_pct"]), 2),
                "Sharpe (tasa 0)": round(float(metrics["sharpe_zero_rate"]), 2),
                "Exposición al mercado (%)": round(float(metrics["market_exposure_pct"]), 2),
            }
        )
    if result.trades.empty:
        st.info("No hubo operaciones cerradas con estas reglas.")
    else:
        st.dataframe(result.trades, width="stretch", hide_index=True)
        st.download_button(
            "Exportar operaciones del backtest",
            result.trades.to_csv(index=False).encode("utf-8"),
            file_name=f"backtest_{ticker}.csv",
            mime="text/csv",
        )


def render_operation_form(
    journal: object,
    *,
    form_key: str,
    fixed_fee: float,
    owner_label: str | None = None,
    flash_key: str = "_journal_flash",
    recorded_by: str = "",
    notes_label: str = "Notas",
) -> None:
    """Formulario común para que un usuario o el administrador registre operaciones."""

    heading = (
        f"Registrar operación para {owner_label}"
        if owner_label
        else "Registrar operación"
    )
    st.subheader(heading)
    with st.form(form_key, clear_on_submit=True):
        ticker = st.text_input("Ticker")
        side = st.selectbox("Tipo", ["Compra", "Venta"])
        quantity = st.number_input(
            "Cantidad",
            min_value=0.000001,
            value=1.0,
            format="%.6f",
        )
        price = st.number_input(
            "Precio",
            min_value=0.000001,
            value=1.0,
            format="%.4f",
        )
        currency = st.selectbox(
            "Moneda",
            ["EUR", "USD", "GBP", "CHF", "JPY", "CAD", "AUD"],
        )
        fees = st.number_input(
            "Comisión pagada",
            min_value=0.0,
            value=float(fixed_fee),
            format="%.2f",
            help="Por defecto se utiliza 1 unidad monetaria por operación.",
        )
        executed_at = st.date_input("Fecha", value=date.today())
        notes = st.text_area(notes_label)
        submitted = st.form_submit_button("Guardar", type="primary")
    if not submitted:
        return
    if not ticker.strip():
        st.error("El ticker es obligatorio.")
        return
    try:
        journal.add_operation(
            ticker,
            side,
            quantity,
            price,
            fees,
            executed_at,
            notes,
            currency=currency,
            recorded_by=recorded_by,
        )
    except (ValueError, JournalStorageError) as exc:
        st.error(str(exc))
        return
    st.session_state[flash_key] = (
        f"Operación guardada para {owner_label}."
        if owner_label
        else f"Operación guardada en {getattr(journal, 'backend_name', 'el diario')}."
    )
    st.rerun()


def operation_history_for_display(operations: pd.DataFrame) -> pd.DataFrame:
    """Presenta el diario con etiquetas sencillas sin alterar los datos exportados."""

    return operations.rename(
        columns={
            "id": "ID",
            "ticker": "Empresa",
            "side": "Tipo",
            "quantity": "Cantidad",
            "price": "Precio",
            "fees": "Comisión",
            "executed_at": "Fecha",
            "notes": "Motivo / notas",
            "currency": "Moneda",
            "recorded_by": "Registrado por",
            "created_at": "Guardado el",
        }
    )


def render_journal(
    prepared: dict[str, pd.DataFrame],
    fundamental_results: dict[str, FundamentalResult],
    opportunity_results: dict[str, OpportunityResult],
    strategy: StrategyConfig,
    fx_snapshot: FxSnapshot,
    journal: object,
    *,
    view_key: str,
    title: str,
    description: str,
    actor_username: str,
    shared: bool = False,
    can_delete_all: bool = False,
) -> None:
    st.subheader(title)
    st.caption(description)
    st.caption(f"Almacenamiento: {getattr(journal, 'backend_name', 'diario')}")
    flash_key = f"_{view_key}_journal_flash"
    flash_message = st.session_state.pop(flash_key, None)
    if flash_message:
        st.success(str(flash_message))
    fixed_fee = st.number_input(
        "Comisión fija por cada compra o venta",
        min_value=0.0,
        value=1.0,
        step=0.5,
        format="%.2f",
        help=(
            "Se interpreta en euros. Cuando hay tipos del BCE, la app lo convierte "
            "a la moneda de la acción."
        ),
        key=f"{view_key}_fixed_fee",
    )
    positions_tab, register_tab, switch_tab, history_tab = st.tabs(
        ["Posiciones analizadas", "Registrar operación", "Comparar un cambio", "Historial"]
    )

    with register_tab:
        render_operation_form(
            journal,
            form_key=f"{view_key}_operation_form",
            fixed_fee=float(fixed_fee),
            owner_label="la cartera del grupo" if shared else None,
            flash_key=flash_key,
            recorded_by=actor_username,
            notes_label="Motivo o acuerdo del grupo" if shared else "Notas",
        )

    with history_tab:
        st.subheader("Histórico")
        operations = journal.list_operations()
        if operations.empty:
            st.info("El diario todavía está vacío.")
        else:
            if shared:
                contributors = sorted(
                    value
                    for value in operations["recorded_by"].fillna("").astype(str).unique()
                    if value
                )
                if contributors:
                    st.caption(
                        "Movimientos registrados por: " + ", ".join(contributors) + "."
                    )
            st.dataframe(
                operation_history_for_display(operations),
                width="stretch",
                hide_index=True,
            )
            st.download_button(
                "Exportar diario a CSV",
                operations.to_csv(index=False).encode("utf-8"),
                file_name=f"diario_{view_key}.csv",
                mime="text/csv",
                key=f"{view_key}_download_history",
            )
            deletable = operations
            if shared and not can_delete_all:
                recorded_by = operations["recorded_by"].fillna("").astype(str).str.lower()
                deletable = operations.loc[recorded_by == actor_username.lower()]
            if deletable.empty:
                if shared:
                    st.caption(
                        "Puedes eliminar únicamente los movimientos que registraste tú."
                    )
            else:
                operation_id = st.selectbox(
                    "ID para eliminar",
                    deletable["id"].tolist(),
                    key=f"{view_key}_delete_operation_id",
                )
                if st.button(
                    "Eliminar operación seleccionada",
                    key=f"{view_key}_delete_operation",
                ):
                    journal.delete_operation(int(operation_id))
                    st.rerun()

    positions = journal.open_positions()
    latest_prices = {
        ticker: float(frame["close"].iloc[-1])
        for ticker, frame in prepared.items()
        if not frame.empty
    }
    _, portfolio_kpis = build_position_dashboard(
        operations,
        positions,
        latest_prices,
        fx_snapshot.rates_per_eur,
        sell_fee_eur=float(fixed_fee),
    )
    analysis_rows: list[dict[str, object]] = []
    details: dict[str, dict[str, object]] = {}
    for position in positions.itertuples(index=False):
        ticker = str(position.ticker)
        key = f"{ticker} · {position.currency}"
        if ticker not in prepared:
            analysis_rows.append(
                {
                    "Ticker": ticker,
                    "Moneda": position.currency,
                    "Cantidad": position.quantity,
                    "Coste medio": position.average_cost,
                    "Lectura": "Faltan precios",
                }
            )
            continue
        frame = prepared[ticker]
        latest_price = float(frame["close"].iloc[-1])
        signal = evaluate_latest_signal(
            frame,
            strategy,
            ticker=ticker,
            entry_price=float(position.average_cost),
        )
        fundamentals = fundamental_results[ticker]
        opportunity = opportunity_results[ticker]
        quote_currency = fundamentals.currency
        comparable = quote_currency is None or quote_currency == position.currency
        try:
            sell_fee_in_position_currency = convert_currency(
                float(fixed_fee),
                "EUR",
                str(position.currency),
                fx_snapshot.rates_per_eur,
            )
        except ValueError:
            sell_fee_in_position_currency = float(fixed_fee)
        valuation = value_holding(
            quantity=float(position.quantity),
            average_cost=float(position.average_cost),
            cost_basis=float(position.cost_basis),
            current_price=latest_price,
            sell_fee=sell_fee_in_position_currency,
        )
        analysis_rows.append(
            {
                "Ticker": ticker,
                "Moneda": position.currency,
                "Cantidad": float(position.quantity),
                "Coste medio": float(position.average_cost),
                "Precio actual": latest_price,
                "Beneficio neto": valuation.net_pnl if comparable else float("nan"),
                "Rentabilidad neta": (
                    valuation.net_return_pct if comparable else float("nan")
                ),
                "Beneficio ya realizado": float(position.realized_pnl),
                "Comisiones pagadas": float(position.paid_fees),
                "Empresa": (
                    float(fundamentals.score)
                    if fundamentals.score is not None
                    else float("nan")
                ),
                "Entrada": signal.score,
                "Oportunidad": opportunity.score,
                "Lectura": signal.position_label,
            }
        )
        details[key] = {
            "position": position,
            "signal": signal,
            "fundamentals": fundamentals,
            "opportunity": opportunity,
            "frame": frame,
            "price": latest_price,
            "valuation": valuation,
            "comparable": comparable,
            "sell_fee": sell_fee_in_position_currency,
        }

    with positions_tab:
        if positions.empty:
            st.info("Registra una compra para empezar a analizar tu cartera.")
        else:
            portfolio_cols = st.columns(5)
            portfolio_cols[0].metric(
                "Capital pendiente",
                f"{portfolio_kpis.invested_eur:,.2f} EUR",
                help="Coste de las posiciones abiertas, incluidas las compras y sus comisiones.",
            )
            portfolio_cols[1].metric(
                "Valor neto actual",
                f"{portfolio_kpis.current_net_value_eur:,.2f} EUR",
                help="Valor de las posiciones con precio disponible, descontando una comisión de salida.",
            )
            portfolio_cols[2].metric(
                "Resultado latente",
                f"{portfolio_kpis.unrealized_pnl_eur:+,.2f} EUR",
                delta=f"{portfolio_kpis.unrealized_return_pct:+.2f}%",
                help="Beneficio o pérdida si se cerrasen ahora las posiciones valoradas.",
            )
            portfolio_cols[3].metric(
                "Resultado realizado",
                f"{portfolio_kpis.realized_pnl_eur:+,.2f} EUR",
                help="Resultado acumulado de las ventas registradas.",
            )
            portfolio_cols[4].metric(
                "Comisiones pagadas",
                f"{portfolio_kpis.fees_eur:,.2f} EUR",
            )
            st.caption(
                f"{portfolio_kpis.priced_positions_count} de "
                f"{portfolio_kpis.open_positions_count} posiciones tienen precio actualizado. "
                "Los totales convierten monedas con el último tipo del BCE disponible."
            )
            if analysis_rows:
                st.dataframe(
                    pd.DataFrame(analysis_rows),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Coste medio": st.column_config.NumberColumn(format="%.2f"),
                        "Precio actual": st.column_config.NumberColumn(format="%.2f"),
                        "Beneficio neto": st.column_config.NumberColumn(format="%+.2f"),
                        "Rentabilidad neta": st.column_config.NumberColumn(format="%+.2f%%"),
                        "Beneficio ya realizado": st.column_config.NumberColumn(format="%+.2f"),
                        "Comisiones pagadas": st.column_config.NumberColumn(format="%.2f"),
                        "Empresa": st.column_config.ProgressColumn(
                            min_value=0, max_value=100, format="%d"
                        ),
                        "Entrada": st.column_config.ProgressColumn(
                            min_value=0, max_value=100, format="%d"
                        ),
                        "Oportunidad": st.column_config.ProgressColumn(
                            min_value=0, max_value=100, format="%d"
                        ),
                    },
                )
            missing = [
                str(row.ticker)
                for row in positions.itertuples(index=False)
                if str(row.ticker) not in prepared
            ]
            if missing:
                st.warning(
                    "Faltan precios para: "
                    + ", ".join(missing)
                    + ". Pulsa «Descargar / actualizar»; las posiciones guardadas se incluyen automáticamente."
                )
            if details:
                selected_position = st.selectbox(
                    "Ver diagnóstico de una posición",
                    list(details),
                    key=f"{view_key}_holding_detail",
                )
                detail = details[selected_position]
                position = detail["position"]
                signal = detail["signal"]
                valuation = detail["valuation"]
                decision_text = {
                    "Mantener": "La tendencia no activa una salida. Mantener bajo vigilancia.",
                    "Reducir": "La tendencia se debilita. Conviene revisar o reducir el riesgo.",
                    "Vender": "La tendencia o el stop están dañados. Salida a estudiar.",
                }.get(signal.position_label, "Revisar la posición.")
                if signal.position_label == "Mantener":
                    st.success(f"**{signal.position_label}:** {decision_text}")
                else:
                    st.warning(f"**{signal.position_label}:** {decision_text}")
                detail_cols = st.columns(4)
                detail_cols[0].metric("Coste total pendiente", f"{position.cost_basis:,.2f}")
                detail_cols[1].metric("Valor neto si vendes", f"{valuation.net_exit_value:,.2f}")
                detail_cols[2].metric("Punto de equilibrio", f"{valuation.break_even_price:,.2f}")
                detail_cols[3].metric("Comisiones ya pagadas", f"{position.paid_fees:,.2f}")
                if not detail["comparable"]:
                    st.error(
                        "La moneda guardada no coincide con la moneda de cotización. "
                        "No se calcula beneficio ni plan de ventas hasta corregirla."
                    )
                    st.stop()
                profit_plan = build_profit_taking_plan(
                    quantity=float(position.quantity),
                    average_cost=float(position.average_cost),
                    current_price=float(detail["price"]),
                    stop_loss_pct=strategy.stop_loss_pct,
                    fee_per_sale=float(detail["sell_fee"]),
                )
                st.subheader("Plan probabilístico de toma de beneficios")
                if profit_plan.suggested_sell_now_quantity > 0:
                    st.success(
                        f"El precio ya ha alcanzado niveles de beneficio. La guía propone "
                        f"estudiar la venta de {profit_plan.suggested_sell_now_quantity:,.3f} "
                        f"unidades ({profit_plan.suggested_sell_now_pct:.0f}%) y mantener "
                        f"{profit_plan.trailing_quantity:,.3f} con protección dinámica."
                    )
                    sale_cols = st.columns(2)
                    sale_cols[0].metric(
                        "Beneficio neto de esa venta al precio actual",
                        f"{profit_plan.net_profit_if_sold_now:+,.2f}",
                    )
                    sale_cols[1].metric(
                        "Comisión descontada",
                        f"{float(detail['sell_fee']):,.2f} {position.currency}",
                    )
                else:
                    st.info(
                        "Todavía no se ha alcanzado el primer nivel de beneficio. "
                        "La guía no propone una venta parcial por objetivo."
                    )
                level_rows = [
                    {
                        "Nivel": level.name,
                        "Precio orientativo": level.target_price,
                        "Cantidad del tramo": level.quantity,
                        "Beneficio neto del tramo": level.net_profit_vs_cost,
                        "Estado": "Alcanzado" if level.reached else "Pendiente",
                    }
                    for level in profit_plan.levels
                ]
                st.dataframe(
                    pd.DataFrame(level_rows),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Precio orientativo": st.column_config.NumberColumn(format="%.2f"),
                        "Cantidad del tramo": st.column_config.NumberColumn(format="%.3f"),
                        "Beneficio neto del tramo": st.column_config.NumberColumn(format="%+.2f"),
                    },
                )
                holding_study = historical_forward_return_study(
                    detail["frame"],
                    current_score=signal.score,
                    horizon_days=strategy.forward_horizon_days,
                )
                if holding_study.reliable:
                    expected_price = float(detail["price"]) * (
                        1 + float(holding_study.median_return_pct) / 100
                    )
                    expected_incremental_profit = (
                        float(position.quantity)
                        * (expected_price - float(detail["price"]))
                        - float(detail["sell_fee"])
                    )
                    estimate_cols = st.columns(3)
                    estimate_cols[0].metric(
                        f"Estimación mediana a {holding_study.horizon_days} sesiones",
                        format_pct(float(holding_study.median_return_pct)),
                    )
                    estimate_cols[1].metric(
                        "Precio estadístico orientativo", f"{expected_price:,.2f}"
                    )
                    estimate_cols[2].metric(
                        "Beneficio incremental estimado",
                        f"{expected_incremental_profit:+,.2f}",
                    )
                    st.caption(
                        f"Basado en {holding_study.samples} casos históricos no solapados. "
                        "Es una estimación retrospectiva, no un beneficio prometido."
                    )
                else:
                    st.caption(
                        "No hay suficientes casos comparables para estimar un beneficio esperado."
                    )
    with switch_tab:
        if not details:
            st.info("Registra y actualiza al menos una posición para comparar cambios.")
        else:
            current_key = st.selectbox(
                "Acción que venderías",
                list(details),
                key=f"{view_key}_switch_current",
            )
            current = details[current_key]
            current_ticker = str(current["position"].ticker)
            alternatives = [ticker for ticker in prepared if ticker != current_ticker]
            if not alternatives:
                st.info("Añade otras empresas al radar para compararlas con tu posición.")
            else:
                candidate_ticker = st.selectbox(
                    "Acción que comprarías",
                    alternatives,
                    key=f"{view_key}_switch_candidate",
                )
                candidate_frame = prepared[candidate_ticker]
                candidate_price = float(candidate_frame["close"].iloc[-1])
                candidate_signal = evaluate_latest_signal(
                    candidate_frame, strategy, ticker=candidate_ticker
                )
                candidate_fundamentals = fundamental_results[candidate_ticker]
                current_currency = str(current["position"].currency)
                candidate_currency = candidate_fundamentals.currency
                if not current["comparable"]:
                    st.error(
                        "La moneda guardada para la posición actual no coincide con su "
                        "moneda de cotización. Corrígela antes de comparar el cambio."
                    )
                elif candidate_currency is None:
                    st.warning(
                        "No se conoce la moneda de la alternativa. Actualiza los datos fundamentales."
                    )
                else:
                    comparison = compare_switch(
                        quantity=float(current["position"].quantity),
                        current_price=float(current["price"]),
                        candidate_price=candidate_price,
                        current_currency=current_currency,
                        candidate_currency=candidate_currency,
                        sell_fee=float(fixed_fee),
                        buy_fee=float(fixed_fee),
                        fx_rates_per_eur=fx_snapshot.rates_per_eur,
                        fee_currency="EUR",
                        fx_as_of=(
                            fx_snapshot.as_of.isoformat()
                            if fx_snapshot.as_of
                            else None
                        ),
                    )
                    if not comparison.compatible_currency:
                        st.error(
                            f"No se calcula el cambio: {current_ticker} está registrado en "
                            f"{current_currency} y {candidate_ticker} cotiza en {candidate_currency}. "
                            "El BCE no ofrece el tipo de cambio necesario."
                        )
                    else:
                        switch_cols = st.columns(5)
                        switch_cols[0].metric("Comisión de venta", f"{fixed_fee:.2f} EUR")
                        switch_cols[1].metric("Comisión de compra", f"{fixed_fee:.2f} EUR")
                        switch_cols[2].metric(
                            "Dinero reinvertido",
                            f"{comparison.cash_invested:,.2f} {candidate_currency}",
                        )
                        switch_cols[3].metric(
                            f"Unidades de {candidate_ticker}",
                            f"{comparison.candidate_quantity:,.4f}",
                        )
                        switch_cols[4].metric(
                            "Ventaja mínima para cubrir costes",
                            f"{comparison.fee_hurdle_pct:.3f}%",
                        )
                        technical_gain = candidate_signal.score - current["signal"].score
                        opportunity_gain = (
                            opportunity_results[candidate_ticker].score
                            - current["opportunity"].score
                        )
                        current_quality = current["fundamentals"].score
                        candidate_quality = candidate_fundamentals.score
                        quality_ok = (
                            current_quality is None
                            or candidate_quality is None
                            or candidate_quality >= current_quality - 5
                        )
                        candidate_is_entry = candidate_signal.label in {
                            "Entrada fuerte",
                            "Entrada interesante",
                        }
                        current_is_weak = current["signal"].position_label in {
                            "Reducir",
                            "Vender",
                        }
                        change_worth_studying = (
                            current_is_weak
                            and candidate_is_entry
                            and technical_gain >= 10
                            and opportunity_gain >= 5
                            and quality_ok
                        )
                        if change_worth_studying:
                            st.success(
                                f"**Cambio para estudiar:** {candidate_ticker} mejora "
                                f"{technical_gain:+d} puntos el momento técnico y "
                                f"{opportunity_gain:+d} puntos la oportunidad conjunta, sin una "
                                "calidad claramente inferior con los datos disponibles."
                            )
                        else:
                            st.info(
                                "**No aparece una ventaja suficientemente clara para cambiar.** "
                                "La aplicación exige deterioro de la posición actual, una entrada "
                                "atractiva, 10 puntos de mejora técnica y 5 de oportunidad conjunta."
                            )
                        if comparison.conversion_rate not in (None, 1.0):
                            st.caption(
                                f"Conversión aplicada: 1 {current_currency} = "
                                f"{comparison.conversion_rate:.6f} {candidate_currency} "
                                f"(BCE {comparison.fx_as_of or 'sin fecha disponible'})."
                            )
                        st.caption(
                            "El cálculo descuenta 1 comisión al vender y 1 al comprar. No incluye "
                            "impuestos, spread, coste adicional de cambio del broker ni la futura "
                            "venta de la nueva acción."
                        )


def render_admin_panel(
    accounts: dict[str, AuthConfig],
    prepared: dict[str, pd.DataFrame],
    fx_snapshot: FxSnapshot,
    admin_username: str,
) -> None:
    """Vista agregada y mantenimiento de las carteras de los usuarios."""

    st.subheader("Administración de carteras")
    st.caption(
        "Vista privada del administrador. Cada usuario sólo puede consultar y modificar "
        "su propia cartera."
    )
    flash_message = st.session_state.pop("_admin_flash", None)
    if flash_message:
        st.success(str(flash_message))

    usernames = managed_usernames(accounts)
    if not usernames:
        st.warning("No hay cuentas de usuario configuradas.")
        return

    fixed_fee = st.number_input(
        "Comisión orientativa para valoraciones administrativas",
        min_value=0.0,
        value=1.0,
        step=0.5,
        format="%.2f",
        key="admin_fixed_fee",
    )
    latest_prices = {
        ticker: float(frame["close"].iloc[-1])
        for ticker, frame in prepared.items()
        if not frame.empty
    }
    snapshots: dict[str, dict[str, object]] = {}
    summary_rows: list[dict[str, object]] = []

    for username in usernames:
        journal = create_journal(username)
        operations = journal.list_operations()
        positions = calculate_open_positions(operations)
        dashboard, kpis = build_position_dashboard(
            operations,
            positions,
            latest_prices,
            fx_snapshot.rates_per_eur,
            sell_fee_eur=float(fixed_fee),
        )
        snapshots[username] = {
            "journal": journal,
            "operations": operations,
            "positions": positions,
            "dashboard": dashboard,
            "kpis": kpis,
        }
        summary_rows.append(
            {
                "Usuario": username,
                "Nombre": accounts[username].display_name,
                "Operaciones": kpis.operations_count,
                "Posiciones": kpis.open_positions_count,
                "Con precio": (
                    f"{kpis.priced_positions_count}/{kpis.open_positions_count}"
                ),
                "Capital pendiente EUR": kpis.invested_eur,
                "Valor neto EUR": kpis.current_net_value_eur,
                "Resultado latente EUR": kpis.unrealized_pnl_eur,
                "Rentabilidad latente": kpis.unrealized_return_pct,
                "Resultado realizado EUR": kpis.realized_pnl_eur,
                "Comisiones EUR": kpis.fees_eur,
                "Última actividad": kpis.latest_activity or "Sin operaciones",
            }
        )

    active_users = sum(
        1 for snapshot in snapshots.values() if len(snapshot["operations"]) > 0
    )
    total_operations = sum(
        int(snapshot["kpis"].operations_count) for snapshot in snapshots.values()
    )
    total_positions = sum(
        int(snapshot["kpis"].open_positions_count) for snapshot in snapshots.values()
    )
    total_invested = sum(
        float(snapshot["kpis"].invested_eur) for snapshot in snapshots.values()
    )
    total_unrealized = sum(
        float(snapshot["kpis"].unrealized_pnl_eur) for snapshot in snapshots.values()
    )
    admin_cols = st.columns(5)
    admin_cols[0].metric("Usuarios", len(usernames))
    admin_cols[1].metric("Con actividad", active_users)
    admin_cols[2].metric("Operaciones", total_operations)
    admin_cols[3].metric("Posiciones abiertas", total_positions)
    admin_cols[4].metric(
        "Resultado latente conjunto",
        f"{total_unrealized:+,.2f} EUR",
        help=f"Sobre {total_invested:,.2f} EUR de capital pendiente convertible.",
    )

    overview_tab, register_tab, detail_tab = st.tabs(
        ["Resumen de usuarios", "Añadir posición", "Detalle e historial"]
    )
    with overview_tab:
        st.dataframe(
            pd.DataFrame(summary_rows),
            width="stretch",
            hide_index=True,
            column_config={
                "Capital pendiente EUR": st.column_config.NumberColumn(format="%.2f"),
                "Valor neto EUR": st.column_config.NumberColumn(format="%.2f"),
                "Resultado latente EUR": st.column_config.NumberColumn(format="%+.2f"),
                "Rentabilidad latente": st.column_config.NumberColumn(format="%+.2f%%"),
                "Resultado realizado EUR": st.column_config.NumberColumn(format="%+.2f"),
                "Comisiones EUR": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        if total_positions and not latest_prices:
            st.info(
                "Pulsa «Descargar / actualizar» para valorar las posiciones con precios "
                "actuales y calcular sus rendimientos."
            )

    with register_tab:
        selected_owner = st.selectbox(
            "Usuario al que pertenece la operación",
            usernames,
            format_func=lambda value: (
                f"{accounts[value].display_name} ({value})"
            ),
            key="admin_register_owner",
        )
        render_operation_form(
            snapshots[selected_owner]["journal"],
            form_key=f"admin_operation_form_{selected_owner}",
            fixed_fee=float(fixed_fee),
            owner_label=accounts[selected_owner].display_name,
            flash_key="_admin_flash",
            recorded_by=admin_username,
        )

    with detail_tab:
        selected_detail = st.selectbox(
            "Usuario que quieres revisar",
            usernames,
            format_func=lambda value: (
                f"{accounts[value].display_name} ({value})"
            ),
            key="admin_detail_owner",
        )
        selected_snapshot = snapshots[selected_detail]
        dashboard = selected_snapshot["dashboard"]
        operations = selected_snapshot["operations"]
        if dashboard.empty:
            st.info("Este usuario no tiene posiciones abiertas.")
        else:
            visible_dashboard = dashboard.rename(
                columns={
                    "ticker": "Ticker",
                    "currency": "Moneda",
                    "quantity": "Cantidad",
                    "average_cost": "Coste medio",
                    "cost_basis": "Capital pendiente",
                    "current_price": "Precio actual",
                    "net_value_eur": "Valor neto EUR",
                    "net_pnl_eur": "Resultado EUR",
                    "net_return_pct": "Rentabilidad",
                    "allocation_pct": "Peso en cartera",
                }
            )
            st.dataframe(
                visible_dashboard,
                width="stretch",
                hide_index=True,
                column_config={
                    "Coste medio": st.column_config.NumberColumn(format="%.2f"),
                    "Capital pendiente": st.column_config.NumberColumn(format="%.2f"),
                    "Precio actual": st.column_config.NumberColumn(format="%.2f"),
                    "Valor neto EUR": st.column_config.NumberColumn(format="%.2f"),
                    "Resultado EUR": st.column_config.NumberColumn(format="%+.2f"),
                    "Rentabilidad": st.column_config.NumberColumn(format="%+.2f%%"),
                    "Peso en cartera": st.column_config.ProgressColumn(
                        min_value=0,
                        max_value=100,
                        format="%.1f%%",
                    ),
                },
            )
        st.markdown("#### Operaciones registradas")
        if operations.empty:
            st.caption("Todavía no hay operaciones.")
        else:
            st.dataframe(operations, width="stretch", hide_index=True)


def render_favorite_list(
    favorites: pd.DataFrame,
    *,
    title: str,
    journal,
    actor_username: str,
    shared: bool = False,
    can_delete_all: bool = False,
) -> None:
    st.markdown(f"#### {title}")
    st.caption(f"{len(favorites)} de {MAX_FAVORITES} empresas guardadas")
    if favorites.empty:
        st.info("Todavía no hay empresas en esta lista.")
        return

    visible = favorites.loc[:, ["name", "ticker", "exchange", "recorded_by"]].rename(
        columns={
            "name": "Empresa",
            "ticker": "Ticker",
            "exchange": "Mercado",
            "recorded_by": "Añadida por",
        }
    )
    if not shared:
        visible = visible.drop(columns=["Añadida por"])
    st.dataframe(visible, width="stretch", hide_index=True)

    removable = favorites
    if shared and not can_delete_all:
        removable = favorites.loc[
            favorites["recorded_by"].fillna("").astype(str).str.lower()
            == actor_username.lower()
        ]
    if removable.empty:
        st.caption("Sólo puedes quitar del grupo las empresas que tú añadiste.")
        return
    options = removable["ticker"].astype(str).tolist()
    labels = {
        str(row.ticker): f"{row.name} ({row.ticker})"
        for row in removable.itertuples(index=False)
    }
    remove_col, button_col = st.columns([3, 1])
    selected = remove_col.selectbox(
        "Quitar de la lista",
        options,
        format_func=lambda ticker: labels.get(ticker, ticker),
        key=f"remove_favorite_{'group' if shared else 'private'}",
        label_visibility="collapsed",
    )
    if button_col.button(
        "Quitar",
        key=f"remove_favorite_button_{'group' if shared else 'private'}",
        width="stretch",
    ):
        try:
            journal.delete_favorite(selected)
        except (JournalStorageError, ValueError) as exc:
            st.error(str(exc))
        else:
            st.success(f"{selected} ya no está en esta lista.")
            st.rerun()


def render_favorites_manager(
    private_journal,
    group_journal,
    private_favorites: pd.DataFrame,
    group_favorites: pd.DataFrame,
    *,
    actor_username: str,
    is_admin: bool,
) -> None:
    st.subheader("Favoritos y buscador de empresas")
    st.write(
        "Busca por el nombre normal de la empresa, guárdala en tu lista o en la del "
        "grupo y después selecciónala en la barra izquierda para analizarla."
    )
    with st.form("company_search_form"):
        search_col, button_col = st.columns([4, 1])
        query = search_col.text_input(
            "Nombre o símbolo",
            placeholder="Ejemplo: Taiwan Semiconductor, Inditex o Microsoft",
            help="El buscador muestra acciones y ETF de los mercados disponibles en Yahoo.",
        )
        submitted = button_col.form_submit_button(
            "Buscar",
            type="primary",
            width="stretch",
        )
    if submitted:
        try:
            st.session_state["favorite_search_results"] = cached_company_search(query)
        except (DataDownloadError, ValueError) as exc:
            st.session_state["favorite_search_results"] = []
            st.error(str(exc))

    results: list[TickerSearchResult] = st.session_state.get(
        "favorite_search_results",
        [],
    )
    if submitted and not results:
        st.warning(
            "No se encontraron acciones o ETF. Prueba con el nombre en inglés o usa "
            "el símbolo manual en la barra izquierda."
        )
    if results:
        result_index = st.selectbox(
            "Resultado correcto",
            options=range(len(results)),
            format_func=lambda index: results[index].label,
            key="favorite_search_result",
        )
        destination = st.radio(
            "Dónde guardarla",
            ["Mi lista privada", "Lista del grupo"],
            horizontal=True,
            help="Guardar una empresa no es una recomendación de compra.",
        )
        selected = results[result_index]
        if st.button(
            f"Guardar {selected.ticker}",
            type="primary",
            key="save_favorite",
        ):
            target = (
                private_journal
                if destination == "Mi lista privada"
                else group_journal
            )
            try:
                target.add_favorite(
                    selected.ticker,
                    selected.name,
                    selected.exchange,
                    recorded_by=actor_username,
                )
            except (JournalStorageError, ValueError) as exc:
                st.error(str(exc))
            else:
                st.success(
                    f"{selected.name} se ha guardado en "
                    f"{destination.lower()}."
                )
                st.session_state.pop("favorite_search_results", None)
                st.rerun()

    st.info(
        "Para no ralentizar la aplicación, puedes guardar 100 favoritas y elegir hasta "
        "25 para análisis completo en cada actualización. Todas las posiciones abiertas "
        "se incorporan automáticamente con una actualización de precio y tendencia."
    )
    private_col, group_col = st.columns(2)
    with private_col:
        render_favorite_list(
            private_favorites,
            title="Mi lista privada",
            journal=private_journal,
            actor_username=actor_username,
        )
    with group_col:
        render_favorite_list(
            group_favorites,
            title="Lista del grupo",
            journal=group_journal,
            actor_username=actor_username,
            shared=True,
            can_delete_all=is_admin,
        )


def render_methodology() -> None:
    st.subheader("Guía sencilla")
    st.markdown(
        """
        **Cómo leer las señales**

        - **Entrada fuerte (75+):** configuración técnica especialmente completa; no garantiza una subida.
        - **Entrada interesante (65+):** reúne suficiente fuerza para estudiarla con más detalle.
        - **Vigilancia (55+):** promete, pero todavía le falta confirmación.
        - **Esperar:** puede ser una buena empresa, pero el precio está demasiado acelerado.
        - **Mantener / Reducir / Vender:** son lecturas separadas para quien ya posee la acción.

        **Diccionario rápido**

        - **Tendencia:** dirección general del precio durante varios meses.
        - **Impulso:** velocidad con la que está subiendo o bajando.
        - **Nuevo máximo o ruptura:** supera una zona que antes no podía superar.
        - **Actividad o volumen:** cuánto se negocia comparado con un día normal.
        - **Peor caída temporal:** mayor pérdida sufrida desde un máximo anterior.
        - **Oportunidad conjunta:** combina calidad, valoración, momento, fortaleza
          frente al mercado y riesgo. No sustituye las cinco notas individuales.
        - **Confianza de datos:** indica cuántas métricas están disponibles; no es
          la probabilidad de ganar dinero.
        - **Cambio de acción:** descuenta una comisión en euros al vender y otra al
          comprar. Si cambia la moneda utiliza el último tipo de referencia del BCE.
        - **R:** riesgo inicial por acción. Con un stop del 8%, 1R equivale al 8%
          del precio medio de compra.
        - **Beneficio esperado:** mediana histórica de señales parecidas, no una
          rentabilidad garantizada.

        La nota de empresa utiliza rentabilidad, crecimiento, deuda y caja. La valoración
        examina múltiplos y generación de caja. La nota de entrada combina tendencia,
        impulso de uno y tres meses, MACD, rupturas, cercanía a máximos, volumen y calidad
        del precio. La fortaleza relativa compara la acción con el mercado y el sector.
        Ninguna de estas notas es una probabilidad calibrada de beneficio.

        **Controles contra sesgos incluidos**

        - Los indicadores sólo usan datos presentes y pasados; no se rellenan hacia atrás.
        - La señal del cierre se ejecuta en la siguiente apertura.
        - El backtest incluye comisión, deslizamiento y gaps a través del stop.
        - Las estimaciones de retorno usan eventos no solapados y no se muestran
          con menos de ocho casos comparables.

        **Limitaciones que siguen abiertas**

        - *Sobreoptimización:* cambiar muchos parámetros hasta maximizar el pasado degrada la validez futura.
          Reserva un periodo fuera de muestra y usa walk-forward antes de extraer conclusiones.
        - *Sesgo de supervivencia:* una lista actual de tickers omite empresas deslistadas o quebradas.
        - *Look-ahead e intradía:* con velas diarias no conocemos la secuencia exacta entre máximo y mínimo.
          El trailing stop actualizado al cierre se aplica desde la sesión siguiente.
        - Yahoo/yfinance continúa siendo la fuente práctica de precios y contexto. Para
          tickers estadounidenses se intentan contrastar las cuentas con SEC EDGAR.
        - La comparación opcional con Alpha Vantage detecta diferencias, pero no convierte
          los datos gratuitos en cotizaciones profesionales en tiempo real.
        - Dividendos, fiscalidad, préstamos de valores y el coste de cambio del broker no están modelados.
        - Los tipos del BCE son referencias informativas y pueden diferir del cambio real del broker.
        - Los niveles de venta y stops son referencias: una orden puede ejecutarse
          a otro precio durante gaps o mercados volátiles.
        """
    )


def main() -> None:
    authenticated_user = require_login()
    accounts = load_auth_accounts()
    try:
        journal = create_journal(authenticated_user.username)
        group_journal = create_journal(GROUP_PORTFOLIO_OWNER)
    except JournalStorageError as exc:
        st.error(str(exc))
        st.stop()
    st.title("Stock Signal Lab")
    role_caption = (
        "Panel administrador"
        if authenticated_user.is_admin
        else f"Espacio de {authenticated_user.display_name}"
    )
    st.caption(
        f"{role_caption} · Buscador de oportunidades · No ejecuta órdenes"
    )
    favorite_storage_error = ""
    try:
        private_favorites = journal.list_favorites()
        group_favorites = group_journal.list_favorites()
    except JournalStorageError as exc:
        private_favorites = pd.DataFrame()
        group_favorites = pd.DataFrame()
        favorite_storage_error = str(exc)
    favorite_tickers, favorite_labels = build_favorite_catalog(
        private_favorites,
        group_favorites,
    )
    (
        tickers,
        start,
        end,
        auto_adjust,
        alpha_vantage_key,
        strategy,
        backtest,
        load_clicked,
    ) = build_sidebar(favorite_tickers, favorite_labels)
    if favorite_storage_error:
        st.sidebar.warning(
            "Los favoritos todavía no están disponibles. Hay que aplicar la "
            "actualización de la base de datos."
        )
    try:
        strategy.validate()
        backtest.validate()
    except ValueError as exc:
        st.error(f"Configuración inválida: {exc}")
        st.stop()

    if load_clicked:
        held_tickers: list[str] = []
        owners_to_load = [authenticated_user.username, GROUP_PORTFOLIO_OWNER]
        if authenticated_user.is_admin:
            owners_to_load.extend(managed_usernames(accounts))
        for owner in dict.fromkeys(owners_to_load):
            try:
                owner_journal = (
                    journal
                    if owner == authenticated_user.username
                    else create_journal(owner)
                )
                saved_positions = owner_journal.open_positions()
            except JournalStorageError as exc:
                st.sidebar.warning(
                    f"No se pudo consultar la cartera de {owner}: {exc}"
                )
                continue
            if not saved_positions.empty:
                held_tickers.extend(saved_positions["ticker"].astype(str).tolist())
        tickers_to_load = list(dict.fromkeys([*tickers, *held_tickers]))
        load_market_data(
            tickers_to_load,
            start,
            end,
            auto_adjust,
            alpha_vantage_key,
            fundamental_tickers=set(tickers),
        )
    for error in st.session_state.get("download_errors", []):
        st.warning(error)

    raw_data: dict[str, pd.DataFrame] = st.session_state.get("market_data", {})
    raw_fundamentals: dict[str, dict[str, object]] = st.session_state.get(
        "fundamental_data", {}
    )
    reference_data: dict[str, pd.DataFrame] = st.session_state.get("reference_data", {})
    price_verifications: dict[str, PriceVerification] = st.session_state.get(
        "price_verifications", {}
    )
    fx_snapshot: FxSnapshot = st.session_state.get(
        "fx_snapshot",
        FxSnapshot(as_of=None, rates_per_eur={"EUR": 1.0}),
    )
    tab_labels = [
        "Oportunidades",
        "Prueba histórica",
        "Favoritos",
        "Mi cartera privada",
        "Cartera del grupo",
    ]
    if authenticated_user.is_admin:
        tab_labels.append("Administración")
    tab_labels.append("Ayuda y riesgos")
    app_tabs = st.tabs(tab_labels)
    tab_analysis, tab_backtest, tab_favorites, tab_private, tab_group = app_tabs[:5]
    if authenticated_user.is_admin:
        tab_admin = app_tabs[5]
        tab_methodology = app_tabs[6]
    else:
        tab_admin = None
        tab_methodology = app_tabs[5]
    (
        prepared,
        summary,
        fundamental_results,
        valuation_results,
        relative_results,
        risk_results,
        opportunity_results,
    ) = (
        prepare_data(raw_data, raw_fundamentals, reference_data, strategy)
        if raw_data
        else ({}, [], {}, {}, {}, {}, {})
    )

    with tab_analysis:
        if not raw_data:
            st.info(
                "Empieza en «Favoritos»: busca empresas por su nombre, selecciónalas "
                "en la barra izquierda y pulsa «Descargar / actualizar»."
            )
        elif not prepared:
            st.error("No hay suficiente histórico válido para generar señales.")
        else:
            st.subheader("Ranking de oportunidades")
            radar = pd.DataFrame(summary)
            if "Momento entrada" in radar.columns:
                radar = radar.sort_values(
                    ["Oportunidad", "Confianza datos", "Momento entrada"],
                    ascending=[False, False, False],
                    na_position="last",
                ).reset_index(drop=True)
                radar.insert(0, "Ranking", range(1, len(radar) + 1))
            st.dataframe(
                radar,
                width="stretch",
                hide_index=True,
                column_config={
                    "Oportunidad": st.column_config.ProgressColumn(
                        min_value=0,
                        max_value=100,
                        format="%d",
                        help="Combinación de las cinco familias de análisis.",
                    ),
                    "Confianza datos": st.column_config.ProgressColumn(
                        min_value=0,
                        max_value=100,
                        format="%d%%",
                        help="Cobertura de datos, no probabilidad de beneficio.",
                    ),
                    "Calidad empresa": st.column_config.ProgressColumn(
                        "Empresa /100",
                        min_value=0,
                        max_value=100,
                        format="%d",
                        help="Rentabilidad, crecimiento, deuda y caja. Vacío significa datos insuficientes.",
                    ),
                    "Momento entrada": st.column_config.ProgressColumn(
                        "Entrada /100",
                        min_value=0,
                        max_value=100,
                        format="%d",
                        help="Tendencia, impulso, MACD, volumen, máximos y calidad del precio de entrada.",
                    ),
                    "Valoración": st.column_config.ProgressColumn(
                        min_value=0,
                        max_value=100,
                        format="%d",
                        help="Precio frente a beneficios, crecimiento, caja y patrimonio.",
                    ),
                    "Fuerza relativa": st.column_config.ProgressColumn(
                        min_value=0,
                        max_value=100,
                        format="%d",
                        help="Comportamiento frente al índice y al sector.",
                    ),
                    "Riesgo controlado": st.column_config.ProgressColumn(
                        min_value=0,
                        max_value=100,
                        format="%d",
                        help="Una nota alta indica menor volatilidad, caída y problemas de liquidez.",
                    ),
                    "Fuerza 3 meses": st.column_config.NumberColumn(
                        "Subida/bajada 3 meses",
                        format="%+.1f%%",
                        help="Cambio del precio durante aproximadamente tres meses.",
                    ),
                    "Desde su máximo": st.column_config.NumberColumn(
                        format="%+.1f%%",
                        help="0% significa que está en máximos; -10% indica que está un 10% por debajo.",
                    ),
                    "Actividad": st.column_config.NumberColumn(
                        format="%.2fx",
                        help="1,20x equivale a un 20% más de negociación que su media.",
                    ),
                },
            )
            alerts = [
                row
                for row in summary
                if row.get("Lectura conjunta") in {"Oportunidad destacada", "Candidata"}
                or row.get("Si ya la tienes") in {"Reducir", "Vender"}
            ]
            opportunities = [
                row
                for row in summary
                if row.get("Lectura conjunta") in {"Oportunidad destacada", "Candidata"}
            ]
            if opportunities:
                leader = max(
                    opportunities, key=lambda item: int(item.get("Oportunidad", 0))
                )
                company_score = leader.get("Calidad empresa")
                company_text = (
                    f"empresa {int(company_score)}/100, "
                    if pd.notna(company_score)
                    else "empresa N/D, "
                )
                st.success(
                    f"Oportunidad destacada: {leader['Ticker']} — {company_text}"
                    f"oportunidad {leader['Oportunidad']}/100, entrada "
                    f"{leader['Momento entrada']}/100 y confianza de datos "
                    f"{leader['Confianza datos']}%. Requiere validación adicional."
                )
            else:
                st.info(
                    "No hay una entrada interesante completa hoy. El ranking permite vigilar "
                    "qué empresas están más cerca de cumplir las condiciones."
                )
            st.caption(f"{len(alerts)} alertas activas basadas en las reglas configuradas.")
            selected = st.selectbox(
                "Empresa que quieres entender mejor", list(prepared), key="analysis_ticker"
            )
            render_analysis(
                selected,
                prepared[selected],
                strategy,
                backtest,
                fundamental_results[selected],
                valuation_results[selected],
                relative_results[selected],
                risk_results[selected],
                opportunity_results[selected],
                raw_fundamentals.get(selected, {}),
                price_verifications.get(selected),
            )

    with tab_backtest:
        if not prepared:
            st.info("Descarga datos antes de ejecutar una simulación.")
        else:
            selected_backtest = st.selectbox("Ticker", list(prepared), key="backtest_select")
            render_backtest(selected_backtest, prepared[selected_backtest], strategy, backtest)

    with tab_favorites:
        if favorite_storage_error:
            st.error(favorite_storage_error)
            st.info(
                "Ejecuta la versión actual de supabase/schema.sql para crear la tabla "
                "de favoritos. Las carteras existentes no se modifican."
            )
        else:
            render_favorites_manager(
                journal,
                group_journal,
                private_favorites,
                group_favorites,
                actor_username=authenticated_user.username,
                is_admin=authenticated_user.is_admin,
            )

    with tab_private:
        if persistent_journal_enabled():
            try:
                render_journal(
                    prepared,
                    fundamental_results,
                    opportunity_results,
                    strategy,
                    fx_snapshot,
                    journal,
                    view_key="private",
                    title="Mi cartera privada",
                    description=(
                        "Sólo tú puedes consultar y modificar estos movimientos. "
                        "No se mezclan con las decisiones del grupo."
                    ),
                    actor_username=authenticated_user.username,
                )
            except JournalStorageError as exc:
                st.error(str(exc))
                st.info(
                    "Comprueba que ejecutaste supabase/schema.sql y que la URL y "
                    "la clave secreta están guardadas en los Secrets de Streamlit."
                )
        else:
            st.warning(
                "El diario está desactivado en este alojamiento porque su disco no "
                "garantiza conservar SQLite tras un reinicio. El análisis y los "
                "backtests siguen disponibles. Para carteras persistentes se necesita "
                "conectar una base de datos externa."
            )

    with tab_group:
        if persistent_journal_enabled():
            try:
                render_journal(
                    prepared,
                    fundamental_results,
                    opportunity_results,
                    strategy,
                    fx_snapshot,
                    group_journal,
                    view_key="group",
                    title="Cartera del grupo",
                    description=(
                        "Compartida entre Luci, Fer, Xavi y ddriu. Todos pueden verla y "
                        "registrar decisiones; no es pública en Internet ni visible sin contraseña."
                    ),
                    actor_username=authenticated_user.username,
                    shared=True,
                    can_delete_all=authenticated_user.is_admin,
                )
            except JournalStorageError as exc:
                st.error(str(exc))
                st.info(
                    "Actualiza la tabla ejecutando supabase/schema.sql para activar "
                    "la identificación de quién registró cada movimiento."
                )
        else:
            st.warning(
                "La cartera compartida necesita almacenamiento persistente."
            )

    if tab_admin is not None:
        with tab_admin:
            if persistent_journal_enabled():
                try:
                    render_admin_panel(
                        accounts,
                        prepared,
                        fx_snapshot,
                        authenticated_user.username,
                    )
                except JournalStorageError as exc:
                    st.error(f"No se pudo cargar el panel administrador: {exc}")
            else:
                st.warning(
                    "El panel multiusuario necesita almacenamiento persistente."
                )

    with tab_methodology:
        render_methodology()

    st.divider()
    st.caption(
        "Señales probabilísticas con fines informativos y educativos. No constituyen asesoramiento financiero "
        "ni garantizan resultados futuros."
    )


if __name__ == "__main__":
    main()
